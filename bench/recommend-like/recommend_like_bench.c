#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <stdbool.h>
#include <stdatomic.h>
#include <pthread.h>
#include <unistd.h>
#include <time.h>
#include <getopt.h>
#include <errno.h>

#include <jemalloc/jemalloc.h>

/* PCG32 (https://www.pcg-random.org/), 单线程使用,无锁 */
typedef struct pcg32_s {
	uint64_t state;
	uint64_t inc;
} pcg32_t;

static inline void
pcg32_init(pcg32_t *rng, uint64_t seed, uint64_t stream) {
	rng->inc = (stream << 1u) | 1u;
	rng->state = 0;
	rng->state = rng->state * 6364136223846793005ULL + rng->inc;
	rng->state += seed;
	rng->state = rng->state * 6364136223846793005ULL + rng->inc;
}

static inline uint32_t
pcg32_next(pcg32_t *rng) {
	uint64_t old = rng->state;
	rng->state = old * 6364136223846793005ULL + rng->inc;
	uint32_t xorshifted = (uint32_t)(((old >> 18u) ^ old) >> 27u);
	uint32_t rot = (uint32_t)(old >> 59u);
	return (xorshifted >> rot) | (xorshifted << ((-rot) & 31));
}

/* [0, bound) 均匀采样;无 modulo bias */
static inline uint32_t
pcg32_bounded(pcg32_t *rng, uint32_t bound) {
	uint32_t threshold = -bound % bound;
	for (;;) {
		uint32_t r = pcg32_next(rng);
		if (r >= threshold) return r % bound;
	}
}

/* [0,1) double */
static inline double
pcg32_double(pcg32_t *rng) {
	return (double)pcg32_next(rng) / 4294967296.0;
}

typedef struct {
	size_t size;
	double weight;
} size_dist_t;

/* 基于 搜推请求应用 stat allocated 比例 (top-15 small + 5 个 16-32K + 2 个 large) */
static const size_dist_t SIZE_DIST[] = {
	{5120,  0.161}, {384,   0.119}, {48,     0.110}, {32,     0.086},
	{1792,  0.053}, {64,    0.052}, {80,     0.051}, {256,    0.041},
	{512,   0.033}, {96,    0.027}, {1280,   0.026}, {128,    0.075},
	{16,    0.024}, {160,   0.022}, {2560,   0.021},
	/* (长尾 small 0.050 已合并到 size=128 槽) */
	{16384, 0.003}, {20480, 0.006}, {24576,  0.010},
	{28672, 0.008}, {32768, 0.007},
	{6291456,  0.005}, {67108864, 0.005},
};
#define SIZE_DIST_N (sizeof(SIZE_DIST) / sizeof(SIZE_DIST[0]))

/* Vose's alias method: O(1) sampling */
typedef struct {
	size_t  *sizes;
	double  *prob;
	uint32_t *alias;
	uint32_t n;
} alias_table_t;

static void
alias_build(alias_table_t *t, const size_dist_t *dist, uint32_t n) {
	/* SIZE_DIST 的 weight 字段语义是 "allocated 字节占比" (spec §2.2)，
	 * 我们要的是 alias 抽样的 "出现次数概率"。转换: count_w = bytes_share / size，
	 * 再归一化。这样跑出来的 cum bytes per size class 才符合 spec 的 byte share. */
	double total = 0;
	double *cw = malloc(n * sizeof(double));
	for (uint32_t i = 0; i < n; i++) {
		cw[i] = dist[i].weight / (double)dist[i].size;
		total += cw[i];
	}

	t->n = n;
	t->sizes = malloc(n * sizeof(size_t));
	t->prob  = malloc(n * sizeof(double));
	t->alias = malloc(n * sizeof(uint32_t));

	double *p = malloc(n * sizeof(double));
	uint32_t *small = malloc(n * sizeof(uint32_t));
	uint32_t *large = malloc(n * sizeof(uint32_t));
	uint32_t ns = 0, nl = 0;

	for (uint32_t i = 0; i < n; i++) {
		t->sizes[i] = dist[i].size;
		p[i] = cw[i] / total * n;
		if (p[i] < 1.0) small[ns++] = i;
		else            large[nl++] = i;
	}
	free(cw);

	while (ns && nl) {
		uint32_t s = small[--ns];
		uint32_t l = large[--nl];
		t->prob[s]  = p[s];
		t->alias[s] = l;
		p[l] = p[l] + p[s] - 1.0;
		if (p[l] < 1.0) small[ns++] = l;
		else            large[nl++] = l;
	}
	while (nl) { uint32_t l = large[--nl]; t->prob[l] = 1.0; t->alias[l] = l; }
	while (ns) { uint32_t s = small[--ns]; t->prob[s] = 1.0; t->alias[s] = s; }

	free(p); free(small); free(large);
}

static inline size_t
alias_sample(const alias_table_t *t, pcg32_t *rng) {
	uint32_t i = pcg32_bounded(rng, t->n);
	if (pcg32_double(rng) < t->prob[i]) return t->sizes[i];
	return t->sizes[t->alias[i]];
}

static void
alias_destroy(alias_table_t *t) {
	free(t->sizes); free(t->prob); free(t->alias);
}

typedef struct {
	void   **live;          /* live ptrs 数组 */
	size_t  *live_size;     /* 每个 ptr 的 size */
	uint32_t cap;           /* live 数组容量 */
	uint32_t n_live;        /* 当前 live 数量 */
	uint64_t allocs;
	uint64_t frees;
	uint64_t alloc_bytes;       /* 累计 alloc 字节 */
	uint64_t alloc_mid_large;   /* 16K-32K 累计 alloc 次数 */
	uint64_t bytes_in_flight;
	pcg32_t  rng;
} worker_t;

typedef struct {
	uint32_t workset_gb;
	uint32_t threads;
	uint32_t duration_s;
	double   churn_rate;
	uint64_t seed;
	const char *csv_path;
	uint32_t stat_print_s;
	alias_table_t at;
	atomic_int   running;
} bench_config_t;

static bench_config_t g_cfg;

static void *
worker_main(void *arg) {
	worker_t *w = (worker_t *)arg;
	size_t target_bytes = (size_t)g_cfg.workset_gb * 1024 * 1024 * 1024 / g_cfg.threads;
	/* alloc 概率 = 1/(1+churn_rate), 使稳态 ndalloc/nmalloc ≈ churn_rate
	 * 例: churn_rate=0.95 → alloc_prob=0.5128, free 比 alloc 少 ~5%, n_live 缓慢增长 */
	double alloc_prob = 1.0 / (1.0 + g_cfg.churn_rate);

	while (atomic_load_explicit(&g_cfg.running, memory_order_relaxed)) {
		double r = pcg32_double(&w->rng);
		bool do_alloc = (r < alloc_prob && w->bytes_in_flight < target_bytes)
		             || (w->n_live == 0);

		if (do_alloc) {
			size_t sz = alias_sample(&g_cfg.at, &w->rng);
			void *p = malloc(sz);
			if (p == NULL) continue;
			memset(p, 0xa5, sz < 64 ? sz : 64);
			if (w->n_live >= w->cap) {
				uint32_t newcap = w->cap * 2;
				void **new_live = realloc(w->live, newcap * sizeof(void *));
				size_t *new_size = realloc(w->live_size, newcap * sizeof(size_t));
				if (new_live == NULL || new_size == NULL) {
					fprintf(stderr, "realloc failed (newcap=%u)\n", newcap);
					abort();
				}
				w->live      = new_live;
				w->live_size = new_size;
				w->cap = newcap;
			}
			w->live[w->n_live]      = p;
			w->live_size[w->n_live] = sz;
			w->n_live++;
			w->bytes_in_flight += sz;
			w->allocs++;
			w->alloc_bytes += sz;
			if (sz >= 16384 && sz <= 32768) {
				w->alloc_mid_large++;
			}
		} else {
			uint32_t idx = pcg32_bounded(&w->rng, w->n_live);
			void *p = w->live[idx];
			size_t sz = w->live_size[idx];
			free(p);
			w->n_live--;
			w->live[idx]      = w->live[w->n_live];
			w->live_size[idx] = w->live_size[w->n_live];
			w->bytes_in_flight -= sz;
			w->frees++;
		}
	}

	/* 不 drain: drain 会把 frees 拉到等于 allocs, 使 ChurnRate 总是 1.0,
	 * 无法反映稳态 churn。in-flight ptrs 让 OS exit 回收 (不影响 jemalloc stats). */
	return NULL;
}

typedef struct {
	uint64_t t_sec;
	/* 内存指标改成 KB 单位（>>10），提升精度便于 KPI 计算 */
	uint64_t rss_kb, active_kb, allocated_kb, dirty_kb;
	uint64_t metadata_kb, edata_kb;
	/* lex_native: 真实 lextents (16K-32K)，64K-page kernel 上恒为 0；
	 * alloc_mid_large: 累计 16K-32K alloc 次数（无视 page size，直接从 worker 计数） */
	uint64_t lex_native;
	uint64_t alloc_mid_large;
	uint64_t cum_allocs, cum_frees;
	uint64_t nmalloc_per_sec, ndalloc_per_sec;
} sample_t;

static uint64_t
mctl_u64(const char *name) {
	uint64_t v = 0;
	size_t sz = sizeof(v);
	if (mallctl(name, &v, &sz, NULL, 0) != 0) {
		size_t v32 = 0; sz = sizeof(v32);
		if (mallctl(name, &v32, &sz, NULL, 0) == 0) v = v32;
	}
	return v;
}

static void
sample_collect(sample_t *s, uint64_t t_sec) {
	/* Refresh epoch to update stats */
	uint64_t epoch = 1;
	size_t sz = sizeof(epoch);
	mallctl("epoch", &epoch, &sz, &epoch, sizeof(epoch));

	s->t_sec        = t_sec;
	s->allocated_kb = mctl_u64("stats.allocated") >> 10;
	s->active_kb    = mctl_u64("stats.active")    >> 10;
	s->metadata_kb  = mctl_u64("stats.metadata")  >> 10;
	s->edata_kb     = mctl_u64("stats.metadata_edata") >> 10;
	uint64_t res = mctl_u64("stats.resident");
	uint64_t act = mctl_u64("stats.active");
	uint64_t meta = mctl_u64("stats.metadata");
	s->dirty_kb     = (res > act + meta) ? (res - act - meta) >> 10 : 0;
	s->rss_kb       = res >> 10;

	/* 16K-32K native curlextents: 64K-page kernel 上恒 0（这些尺寸是 small slab）。
	 * 仍然采样, 4K-page kernel 上仍可作为参考。覆盖 size class index 0-4 (large index space). */
	uint64_t lex = 0;
	for (uint32_t ind = 0; ind <= 4; ind++) {
		char path[128];
		snprintf(path, sizeof path,
		    "stats.arenas.%u.lextents.%u.curlextents", (unsigned)MALLCTL_ARENAS_ALL, ind);
		lex += mctl_u64(path);
	}
	s->lex_native = lex;
}

static FILE *g_csv = NULL;

static void
csv_header(FILE *f) {
	fprintf(f, "t_sec,rss_kb,active_kb,allocated_kb,dirty_kb,metadata_kb,"
	          "edata_kb,lex_native,alloc_mid_large,cum_allocs,cum_frees,"
	          "nmalloc_per_sec,ndalloc_per_sec\n");
	fflush(f);
}

static void
csv_row(FILE *f, const sample_t *s) {
	fprintf(f, "%lu,%lu,%lu,%lu,%lu,%lu,%lu,%lu,%lu,%lu,%lu,%lu,%lu\n",
	    (unsigned long)s->t_sec, (unsigned long)s->rss_kb,
	    (unsigned long)s->active_kb, (unsigned long)s->allocated_kb,
	    (unsigned long)s->dirty_kb, (unsigned long)s->metadata_kb,
	    (unsigned long)s->edata_kb, (unsigned long)s->lex_native,
	    (unsigned long)s->alloc_mid_large,
	    (unsigned long)s->cum_allocs, (unsigned long)s->cum_frees,
	    (unsigned long)s->nmalloc_per_sec, (unsigned long)s->ndalloc_per_sec);
	fflush(f);
}

static void
sample_collect_full(sample_t *s, worker_t *workers, uint64_t t_sec,
    uint64_t *prev_alloc, uint64_t *prev_free, uint32_t dt) {
	uint64_t alloc = 0, freec = 0, mid_large = 0;
	for (uint32_t i = 0; i < g_cfg.threads; i++) {
		alloc     += workers[i].allocs;
		freec     += workers[i].frees;
		mid_large += workers[i].alloc_mid_large;
	}
	sample_collect(s, t_sec);
	s->cum_allocs = alloc;
	s->cum_frees  = freec;
	s->alloc_mid_large = mid_large;
	if (dt == 0) dt = 1;
	s->nmalloc_per_sec = (alloc - *prev_alloc) / dt;
	s->ndalloc_per_sec = (freec - *prev_free)  / dt;
	*prev_alloc = alloc; *prev_free = freec;

	fprintf(stderr,
	    "[t=%lus] rss=%luKB active=%luKB allocated=%luKB dirty=%luKB "
	    "edata=%luKB lex_native=%lu mid_large=%lu mops=%.2f\n",
	    (unsigned long)t_sec, (unsigned long)s->rss_kb,
	    (unsigned long)s->active_kb, (unsigned long)s->allocated_kb,
	    (unsigned long)s->dirty_kb, (unsigned long)s->edata_kb,
	    (unsigned long)s->lex_native, (unsigned long)s->alloc_mid_large,
	    (double)s->nmalloc_per_sec / 1e6);
	if (g_csv) csv_row(g_csv, s);
}

static void *
sampler_main(void *arg) {
	worker_t *workers = (worker_t *)arg;
	uint64_t t = 0;
	uint64_t prev_alloc = 0, prev_free = 0;
	while (1) {
		sleep(g_cfg.stat_print_s);
		t += g_cfg.stat_print_s;
		sample_t s;
		sample_collect_full(&s, workers, t, &prev_alloc, &prev_free,
		    g_cfg.stat_print_s);
		/* 采完再判断 running, 确保最后一行也被记录 */
		if (!atomic_load_explicit(&g_cfg.running, memory_order_relaxed)) {
			break;
		}
	}
	return NULL;
}

static void
print_usage(const char *prog) {
	fprintf(stderr,
	    "Usage: %s [options]\n"
	    "  --workset GB       工作集大小 (default: 8)\n"
	    "  --threads N        工作线程数 (default: 32)\n"
	    "  --duration S       运行时长秒 (default: 120)\n"
	    "  --churn-rate F     目标 ndalloc/nmalloc (default: 0.95)\n"
	    "  --seed X           随机种子 (default: 0=time)\n"
	    "  --stat-print N     每 N 秒打印 jemalloc stats (default: 30)\n"
	    "  --csv FILE         CSV 时序输出路径\n"
	    "  --help\n", prog);
}

static void
parse_args(int argc, char *argv[]) {
	g_cfg.workset_gb   = 8;
	g_cfg.threads      = 32;
	g_cfg.duration_s   = 120;
	g_cfg.churn_rate   = 0.95;
	g_cfg.seed         = 0;
	g_cfg.stat_print_s = 30;
	g_cfg.csv_path     = NULL;

	static struct option opts[] = {
		{"workset",     required_argument, 0, 'w'},
		{"threads",     required_argument, 0, 't'},
		{"duration",    required_argument, 0, 'd'},
		{"churn-rate",  required_argument, 0, 'c'},
		{"seed",        required_argument, 0, 's'},
		{"stat-print",  required_argument, 0, 'p'},
		{"csv",         required_argument, 0, 'C'},
		{"help",        no_argument,       0, 'h'},
		{0, 0, 0, 0}
	};

	int opt;
	while ((opt = getopt_long(argc, argv, "", opts, NULL)) != -1) {
		switch (opt) {
		case 'w': g_cfg.workset_gb   = (uint32_t)atoi(optarg); break;
		case 't': g_cfg.threads      = (uint32_t)atoi(optarg); break;
		case 'd': g_cfg.duration_s   = (uint32_t)atoi(optarg); break;
		case 'c': g_cfg.churn_rate   = atof(optarg); break;
		case 's': g_cfg.seed         = (uint64_t)strtoull(optarg, NULL, 10); break;
		case 'p': g_cfg.stat_print_s = (uint32_t)atoi(optarg); break;
		case 'C': g_cfg.csv_path     = optarg; break;
		case 'h': print_usage(argv[0]); exit(0);
		default:  print_usage(argv[0]); exit(2);
		}
	}
	if (g_cfg.seed == 0) g_cfg.seed = (uint64_t)time(NULL);

	fprintf(stderr,
	    "config: workset=%uGB threads=%u duration=%us churn=%.2f seed=%lu csv=%s\n",
	    g_cfg.workset_gb, g_cfg.threads, g_cfg.duration_s, g_cfg.churn_rate,
	    (unsigned long)g_cfg.seed, g_cfg.csv_path ? g_cfg.csv_path : "(none)");
}

int
main(int argc, char *argv[]) {
	parse_args(argc, argv);
	alias_build(&g_cfg.at, SIZE_DIST, SIZE_DIST_N);

	worker_t *workers = calloc(g_cfg.threads, sizeof(worker_t));
	pthread_t *threads = calloc(g_cfg.threads, sizeof(pthread_t));

	for (uint32_t i = 0; i < g_cfg.threads; i++) {
		workers[i].cap = 65536;
		workers[i].live      = malloc(workers[i].cap * sizeof(void *));
		workers[i].live_size = malloc(workers[i].cap * sizeof(size_t));
		pcg32_init(&workers[i].rng, g_cfg.seed, i + 1);
	}

	atomic_store(&g_cfg.running, 1);
	for (uint32_t i = 0; i < g_cfg.threads; i++) {
		pthread_create(&threads[i], NULL, worker_main, &workers[i]);
	}

	if (g_cfg.csv_path) {
		g_csv = fopen(g_cfg.csv_path, "w");
		if (g_csv == NULL) { perror("fopen csv"); exit(1); }
		csv_header(g_csv);
	}
	pthread_t sampler;
	pthread_create(&sampler, NULL, sampler_main, workers);

	sleep(g_cfg.duration_s);
	atomic_store(&g_cfg.running, 0);

	uint64_t total_allocs = 0, total_frees = 0;
	for (uint32_t i = 0; i < g_cfg.threads; i++) {
		pthread_join(threads[i], NULL);
		total_allocs += workers[i].allocs;
		total_frees  += workers[i].frees;
	}

	fprintf(stderr, "TOTAL: allocs=%llu frees=%llu churn=%.4f mops=%.2f\n",
	    (unsigned long long)total_allocs, (unsigned long long)total_frees,
	    total_allocs ? (double)total_frees / total_allocs : 0.0,
	    (double)total_allocs / 1e6 / g_cfg.duration_s);

	pthread_join(sampler, NULL);
	if (g_csv) fclose(g_csv);

	/* 退出前打印完整 jemalloc stats */
	malloc_stats_print(NULL, NULL, "");

	for (uint32_t i = 0; i < g_cfg.threads; i++) {
		free(workers[i].live);
		free(workers[i].live_size);
	}
	free(workers); free(threads);
	alias_destroy(&g_cfg.at);
	return 0;
}
