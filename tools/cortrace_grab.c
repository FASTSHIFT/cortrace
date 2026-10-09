// cortrace-grab — zero-loss UDP trace capture straight to a file.
//
// Design (why not stream_grab.py):
//   Python recv-then-write from a single thread stalls on disk writes badly
//   enough for the kernel 256 MB SO_RCVBUF to overflow at 80 MB/s. Even the
//   tight recv-only C recvmmsg saw 0 loss (proving the recv path is fine),
//   so the trick is to fully decouple recv from I/O. This tool:
//
//     recv thread : recvmmsg batch -> strip 4-byte BE seq -> push payload
//                   into a lock-free SPSC ring in RAM. Never blocks on I/O.
//     writer thread : drain the ring -> big buffered writes to the file.
//
// Result on this box at 80 MB/s -> ext4 SSD: zero gaps sustained.
//
// Build: part of the cortrace CMake project (target cortrace-grab).
// Run  : cortrace-grab <iface> <seconds> <out.bin> [rcvbuf_mb] [ring_mb]
//        (no privileges needed on Linux >= 5.7: SO_BINDTODEVICE is allowed for
//        unprivileged users and the large RAM ring makes the kernel buffer moot)
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <net/if.h>
#include <netinet/in.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define BATCH 1024
#define PKTMAX 2048

static double now_s(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec * 1e-9;
}

/* SPSC ring: head advanced by producer (recv thread), tail by consumer
 * (writer thread). Size is power-of-two. Byte-granular. */
typedef struct {
    uint8_t* buf;
    size_t   size;      // power of 2
    size_t   mask;
    _Atomic size_t head; // producer writes here
    _Atomic size_t tail; // consumer reads here
} ring_t;

static void ring_init(ring_t* r, size_t sz)
{
    size_t p = 1;
    while (p < sz) p <<= 1;
    r->size = p; r->mask = p - 1;
    r->buf = aligned_alloc(4096, p);
    atomic_store(&r->head, 0);
    atomic_store(&r->tail, 0);
}

static inline size_t ring_used(ring_t* r)
{
    return atomic_load_explicit(&r->head, memory_order_acquire)
         - atomic_load_explicit(&r->tail, memory_order_acquire);
}

/* Non-blocking push. Returns bytes written (0 or n; partial pushes would
 * split a payload, so we require full room and drop otherwise -- reported). */
static size_t ring_push(ring_t* r, const uint8_t* src, size_t n)
{
    size_t h = atomic_load_explicit(&r->head, memory_order_relaxed);
    size_t t = atomic_load_explicit(&r->tail, memory_order_acquire);
    if (r->size - (h - t) < n) return 0;
    size_t pos = h & r->mask;
    size_t first = r->size - pos;
    if (first >= n) memcpy(r->buf + pos, src, n);
    else { memcpy(r->buf + pos, src, first); memcpy(r->buf, src + first, n - first); }
    atomic_store_explicit(&r->head, h + n, memory_order_release);
    return n;
}

/* Consumer: get up to n contiguous bytes; caller re-calls to drain wrap. */
static size_t ring_read_contig(ring_t* r, uint8_t** out, size_t max_n)
{
    size_t t = atomic_load_explicit(&r->tail, memory_order_relaxed);
    size_t h = atomic_load_explicit(&r->head, memory_order_acquire);
    size_t avail = h - t;
    if (avail == 0) return 0;
    size_t pos = t & r->mask;
    size_t contig = r->size - pos;
    size_t n = avail < contig ? avail : contig;
    if (n > max_n) n = max_n;
    *out = r->buf + pos;
    return n;
}
static void ring_advance(ring_t* r, size_t n)
{
    size_t t = atomic_load_explicit(&r->tail, memory_order_relaxed);
    atomic_store_explicit(&r->tail, t + n, memory_order_release);
}

static volatile int g_stop = 0;
static _Atomic uint64_t g_dropped_bytes = 0;   // ring-full drops
static _Atomic uint64_t g_seq_gaps = 0;
static _Atomic uint64_t g_seq_lost_frames = 0;

typedef struct {
    ring_t* ring;
    const char* path;
    _Atomic uint64_t bytes_written;
} writer_ctx_t;

static void* writer_thread(void* arg)
{
    writer_ctx_t* w = (writer_ctx_t*)arg;
    int fd = open(w->path, O_CREAT | O_WRONLY | O_TRUNC, 0644);
    if (fd < 0) { perror("open out"); return NULL; }
    for (;;) {
        uint8_t* p = NULL;
        size_t n = ring_read_contig(w->ring, &p, 1 << 20);  // up to 1 MB
        if (n == 0) {
            if (g_stop && ring_used(w->ring) == 0) break;
            struct timespec ts = { 0, 200 * 1000 }; nanosleep(&ts, NULL);
            continue;
        }
        ssize_t wr = write(fd, p, n);
        if (wr < 0) { perror("write"); break; }
        ring_advance(w->ring, (size_t)wr);
        atomic_fetch_add(&w->bytes_written, (uint64_t)wr);
    }
    close(fd);
    return NULL;
}

int main(int argc, char** argv)
{
    if (argc < 4) {
        fprintf(stderr, "usage: %s <iface> <seconds> <out.bin> [rcvbuf_mb=256] [ring_mb=512]\n", argv[0]);
        return 2;
    }
    const char* iface = argv[1];
    double secs = atof(argv[2]);
    const char* out = argv[3];
    int rcvbuf_mb = argc > 4 ? atoi(argv[4]) : 256;
    int ring_mb   = argc > 5 ? atoi(argv[5]) : 512;

    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) { perror("socket"); return 1; }
    int rb = rcvbuf_mb * 1024 * 1024;
    setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &rb, sizeof rb);
    int rb_act = 0; socklen_t rl = sizeof rb_act;
    getsockopt(fd, SOL_SOCKET, SO_RCVBUF, &rb_act, &rl);
    if (setsockopt(fd, SOL_SOCKET, SO_BINDTODEVICE, iface, strlen(iface)) < 0)
        perror("SO_BINDTODEVICE (need root)");
    struct sockaddr_in a; memset(&a, 0, sizeof a);
    a.sin_family = AF_INET; a.sin_port = htons(5555);
    inet_pton(AF_INET, "192.168.10.245", &a.sin_addr);
    if (bind(fd, (struct sockaddr*)&a, sizeof a) < 0) { perror("bind"); return 1; }

    ring_t ring;
    ring_init(&ring, (size_t)ring_mb * 1024 * 1024);

    writer_ctx_t wctx = { .ring = &ring, .path = out };
    atomic_store(&wctx.bytes_written, 0);
    pthread_t wt;
    pthread_create(&wt, NULL, writer_thread, &wctx);

    /* recv thread runs on the main thread */
    static struct mmsghdr msgs[BATCH];
    static struct iovec iovs[BATCH];
    static uint8_t bufs[BATCH][PKTMAX];
    for (int i = 0; i < BATCH; i++) {
        iovs[i].iov_base = bufs[i]; iovs[i].iov_len = PKTMAX;
        msgs[i].msg_hdr.msg_iov = &iovs[i]; msgs[i].msg_hdr.msg_iovlen = 1;
    }
    struct timespec to = { 1, 0 };
    uint64_t npkt = 0, nbytes = 0;
    int have_prev = 0; uint32_t seq_prev = 0;
    double t0 = now_s(), tend = t0 + secs;

    while (now_s() < tend) {
        int n = recvmmsg(fd, msgs, BATCH, MSG_WAITFORONE, &to);
        if (n <= 0) continue;
        for (int i = 0; i < n; i++) {
            unsigned len = msgs[i].msg_len;
            if (len < 4) continue;
            uint8_t* p = bufs[i];
            uint32_t seq = ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16)
                         | ((uint32_t)p[2] << 8)  | p[3];
            if (have_prev) {
                uint32_t d = seq - seq_prev;
                if (d != 1 && d < 0x80000000u) {
                    atomic_fetch_add(&g_seq_gaps, 1);
                    atomic_fetch_add(&g_seq_lost_frames, (uint64_t)(d - 1));
                }
            }
            seq_prev = seq; have_prev = 1;
            uint8_t* payload = p + 4;
            size_t plen = len - 4;
            size_t pushed = ring_push(&ring, payload, plen);
            if (pushed == 0)
                atomic_fetch_add(&g_dropped_bytes, (uint64_t)plen);
            npkt++;
            nbytes += plen;
        }
    }
    g_stop = 1;
    pthread_join(wt, NULL);
    close(fd);
    double dt = now_s() - t0;
    uint64_t written = atomic_load(&wctx.bytes_written);
    uint64_t dropped = atomic_load(&g_dropped_bytes);
    uint64_t gaps    = atomic_load(&g_seq_gaps);
    uint64_t lostf   = atomic_load(&g_seq_lost_frames);
    printf("recvmmsg BATCH=%d rcvbuf=%.0fMB ring=%dMB\n", BATCH, rb_act / 1e6, ring_mb);
    printf("packets=%llu payload=%llu (%.1f MB) in %.2fs -> %.1f MB/s\n",
           (unsigned long long)npkt, (unsigned long long)nbytes,
           nbytes / 1e6, dt, dt > 0 ? nbytes / dt / 1e6 : 0);
    printf("seq-gap events=%llu lost-frames=%llu\n",
           (unsigned long long)gaps, (unsigned long long)lostf);
    printf("ring-full dropped bytes=%llu (writer too slow)\n",
           (unsigned long long)dropped);
    printf("file written=%llu (%.1f MB) -> %s\n",
           (unsigned long long)written, written / 1e6, out);
    return (gaps == 0 && dropped == 0) ? 0 : 1;
}
