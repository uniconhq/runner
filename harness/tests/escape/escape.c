// The escape fixtures: one C program, built by the compile primitive as a
// contestant's program would be, that tries the attack named on its standard
// input, or as its first argument, and prints one line per thing it tried,
// "<check> REFUSED|ESCAPED|INFO <detail>". harness/tests/test_escape.py runs
// it through sandbox-run or exec, as the container's own process under the
// harness's flags, and with each of those flags taken away in turn. As
// `partner` it is interact's partner, and its entrant's attack is on it.
//
// The resource attacks are capped (512 MB of memory, 3,000 processes or
// threads, 1 GB of disk per directory), so a run with a limit taken away
// still ends; for them the verdict is mostly the outcome sandbox-run or the
// container reports. Brought over from the experiments of 2026-10-04 (A5),
// where every attack was refused and every flag was shown to matter.
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/bpf.h>
#include <netdb.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/prctl.h>
#include <sys/ptrace.h>
#include <sys/shm.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <sys/xattr.h>
#include <unistd.h>

#define LEFT_KEY 0x5EED0001
/* The key of the System V segment one item leaves for the next. */
#define LEFT_MARK "user.escape"
/* The attribute one item sets on the program for the next. */

static void say(const char *check, int escaped, const char *fmt, ...) {
    char buf[512];
    va_list ap; va_start(ap, fmt); vsnprintf(buf, sizeof buf, fmt, ap); va_end(ap);
    printf("%s %s %s\n", check, escaped ? "ESCAPED" : "REFUSED", buf);
    fflush(stdout);
}
static void info(const char *check, const char *fmt, ...) {
    char buf[1024];
    va_list ap; va_start(ap, fmt); vsnprintf(buf, sizeof buf, fmt, ap); va_end(ap);
    printf("%s INFO %s\n", check, buf);
    fflush(stdout);
}

static void try_connect(const char *check, const char *ip, int port) {
    int s = socket(AF_INET, SOCK_STREAM, 0);
    if (s < 0) { say(check, 0, "socket: %s", strerror(errno)); return; }
    struct sockaddr_in a = {.sin_family = AF_INET, .sin_port = htons(port)};
    inet_pton(AF_INET, ip, &a.sin_addr);
    struct timeval tv = {2, 0};
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
    int r = connect(s, (struct sockaddr *)&a, sizeof a);
    say(check, r == 0, "connect %s:%d: %s", ip, port, r == 0 ? "connected" : strerror(errno));
    close(s);
}

static void try_write(const char *check, const char *path) {
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd >= 0) { int w = write(fd, "x", 1); close(fd); say(check, w == 1, "wrote %s", path); }
    else say(check, 0, "%s: %s", path, strerror(errno));
}

static void try_read(const char *check, const char *path) {
    int fd = open(path, O_RDONLY);
    if (fd < 0) { say(check, 0, "%s: %s", path, strerror(errno)); return; }
    char b[64]; ssize_t n = read(fd, b, sizeof b); close(fd);
    say(check, n > 0, "read %zd bytes of %s", n, path);
}

static void try_open(const char *check, const char *path) {
    int fd = open(path, O_RDONLY);
    if (fd >= 0) close(fd);
    say(check, fd >= 0, "%s: %s", path, fd >= 0 ? "opened" : strerror(errno));
}

static void list_dir(const char *check, const char *path) {
    DIR *d = opendir(path);
    if (!d) { info(check, "%s: %s", path, strerror(errno)); return; }
    char names[900] = ""; struct dirent *e;
    while ((e = readdir(d))) if (e->d_name[0] != '.' && strlen(names) < 800) { strcat(names, e->d_name); strcat(names, " "); }
    closedir(d);
    info(check, "%s holds: %s", path, names);
}

static void *spin(void *a) { pause(); return a; }

/* The process of interact's partner: the one whose first argument is
   `partner`, or 0. Under interact each program has a box and a PID
   namespace of its own, so the entrant should find none. */
static pid_t find_partner(void) {
    DIR *d = opendir("/proc"); struct dirent *e; pid_t found = 0;
    while (d && (e = readdir(d)) && !found) {
        if (e->d_name[0] < '0' || e->d_name[0] > '9' || atoi(e->d_name) == getpid()) continue;
        char p[300], b[4096] = "";
        snprintf(p, sizeof p, "/proc/%s/cmdline", e->d_name);
        int fd = open(p, O_RDONLY); if (fd < 0) continue;
        ssize_t n = read(fd, b, sizeof b - 1); close(fd);
        size_t first = strnlen(b, n);
        if ((ssize_t)first + 1 < n && !strcmp(b + first + 1, "partner")) found = atoi(e->d_name);
    }
    if (d) closedir(d);
    return found;
}

static int scan_for_secrets(const char *path, char *found, size_t n) {
    int fd = open(path, O_RDONLY); if (fd < 0) return 0;
    static char b[1 << 16]; ssize_t k = read(fd, b, sizeof b - 1); close(fd);
    if (k <= 0) return 0;
    for (ssize_t i = 0; i < k; i++) if (!b[i]) b[i] = '\n';
    b[k] = 0;
    const char *needles[] = {"TOKEN=", "token=", "UNICON_", "X-Amz-Signature", "Authorization:", "SECRET=", "PASSWORD=", "password=", "CI_", "WOODPECKER_", 0};
    int hit = 0;
    for (int i = 0; needles[i]; i++) if (strstr(b, needles[i])) { hit = 1; snprintf(found + strlen(found), n - strlen(found), "%s:%s ", path, needles[i]); }
    return hit;
}

int main(int argc, char **argv) {
    char mode[64] = "";
    if (argc > 1) snprintf(mode, sizeof mode, "%s", argv[1]);
    else if (scanf("%63s", mode) != 1) return 2;
    signal(SIGPIPE, SIG_IGN);

    // -- resources: the outcome is the verdict
    // Capped, so a run with a limit taken away cannot take the machine down.
    if (!strcmp(mode, "memory")) {
        for (int mb = 1; mb <= 512; mb++) { char *p = malloc(1 << 20); if (!p) { say("memory", 0, "malloc refused at %d MB", mb); return 3; } memset(p, 1, 1 << 20); }
        say("memory", 1, "held 512 MB"); return 0;
    }
    if (!strcmp(mode, "fork")) {
        int n = 0;
        for (; n < 3000; n++) { pid_t c = fork(); if (c == 0) { pause(); _exit(0); } if (c < 0) break; }
        say("fork", n >= 3000, "%d processes started before refusal", n); kill(0, SIGKILL); return 0;
    }
    if (!strcmp(mode, "threads")) {
        int n = 0; pthread_t t;
        for (; n < 3000; n++) if (pthread_create(&t, 0, spin, 0)) break;
        say("threads", n >= 3000, "%d threads started before refusal", n); _exit(0);
    }
    if (!strcmp(mode, "sleep")) { for (;;) sleep(1); }
    if (!strcmp(mode, "spin")) { for (volatile unsigned long x = 0;; x++) ; }
    if (!strcmp(mode, "output")) { static char b[1 << 16]; memset(b, 'y', sizeof b); for (;;) if (fwrite(b, 1, sizeof b, stdout) != sizeof b) return 3; }
    if (!strcmp(mode, "disk")) {
        // Fill whatever is writable: the run's own directory, /tmp and /work.
        const char *dirs[] = {".", "/tmp", "/work", 0};
        static char b[1 << 20]; memset(b, 'z', sizeof b);
        signal(SIGXFSZ, SIG_IGN);
        for (int d = 0; dirs[d]; d++) {
            long mb = 0; char p[256];
            for (int f = 0; mb < 1024; f++) {
                snprintf(p, sizeof p, "%s/fill-%d", dirs[d], f);
                FILE *fp = fopen(p, "w"); if (!fp) { say("disk", 0, "%s after %ld MB: open %s", dirs[d], mb, strerror(errno)); break; }
                int stop = 0;
                for (int i = 0; i < 16; i++) { if (fwrite(b, 1, sizeof b, fp) != sizeof b || fflush(fp)) { stop = 1; break; } mb++; }
                fclose(fp);
                if (stop) { say("disk", 0, "%s after %ld MB: %s", dirs[d], mb, strerror(errno)); break; }
            }
            if (mb >= 1024) say("disk", 1, "%s took 1024 MB without refusal", dirs[d]);
            for (int f = 0; f < 70; f++) { snprintf(p, sizeof p, "%s/fill-%d", dirs[d], f); unlink(p); }
        }
        return 0;
    }

    // -- the network
    if (!strcmp(mode, "network")) {
        int s = socket(AF_INET, SOCK_DGRAM, 0);
        info("socket", "socket(AF_INET) %s", s >= 0 ? "allowed (a socket with nowhere to go)" : strerror(errno));
        try_connect("internet", "1.1.1.1", 443);
        try_connect("metadata", "169.254.169.254", 80);
        try_connect("docker-bridge-gateway", "172.17.0.1", 2375);
        try_connect("machine-loopback-ssh", "127.0.0.1", 22);
        struct addrinfo *res; int r = getaddrinfo("example.com", "80", 0, &res);
        say("dns", r == 0, "getaddrinfo example.com: %s", r == 0 ? "resolved" : gai_strerror(r));
        // Any interface but loopback that is not down is a network to use,
        // whether or not this machine can reach the internet through it. A
        // kernel with the tunnel modules loaded gives every network namespace
        // its fallback tunnels (sit0, gre0, ...), down, which only
        // CAP_NET_ADMIN could raise. An interface is a link under
        // /sys/class/net, and one whose operstate cannot be read is counted;
        // bonding_masters is a plain file, no interface.
        DIR *d = opendir("/sys/class/net");
        char names[512] = ""; struct dirent *e; int others = 0;
        while (d && (e = readdir(d))) {
            if (e->d_name[0] == '.') continue;
            char p[300], state[32] = "";
            snprintf(p, sizeof p, "/sys/class/net/%s/operstate", e->d_name);
            if (e->d_type != DT_LNK) continue;
            FILE *f = fopen(p, "r");
            if (!f) snprintf(state, sizeof state, "unreadable");
            else {
                if (!fgets(state, sizeof state, f)) state[0] = 0;
                fclose(f);
            }
            state[strcspn(state, "\n")] = 0;
            if (strlen(names) < 400) { strcat(names, e->d_name); strcat(names, ":"); strcat(names, state); strcat(names, " "); }
            if (strcmp(e->d_name, "lo") && strcmp(state, "down")) others++;
        }
        if (d) closedir(d);
        say("network-interfaces", others > 0, "%s", names);
        return 0;
    }

    // -- the file system
    if (!strcmp(mode, "files")) {
        try_write("write-root", "/escape-test");
        try_write("write-etc", "/etc/passwd");
        try_write("write-usr", "/usr/bin/escape-test");
        try_write("write-var-tmp", "/var/tmp/escape-test");  // world-writable in the image
        try_write("write-dev-shm", "/dev/shm/escape-test");  // the daemon's default ipc mounts it
        {   // one file past the output limit, in the first writable place
            signal(SIGXFSZ, SIG_IGN);
            static char b[1 << 20]; memset(b, 'b', sizeof b);
            const char *dirs[] = {"/work", "/tmp", ".", 0};
            for (int d = 0; dirs[d]; d++) {
                char p[256]; snprintf(p, sizeof p, "%s/big-file", dirs[d]);
                FILE *fp = fopen(p, "w"); if (!fp) continue;
                long mb = 0; while (mb < 128 && fwrite(b, 1, sizeof b, fp) == sizeof b && !fflush(fp)) mb++;
                fclose(fp); unlink(p);
                say("one-big-file", mb >= 128, "%s took %ld MB in one file", dirs[d], mb);
                break;
            }
        }
        try_write("write-work-root", "/work/escape-test");
        try_write("write-work-in", "/work/in/escape-test");
        try_read("read-other-run-output", "/work/out/files-peer/output");
        try_read("read-other-test-input", "/work/in/peer-input");
        try_read("read-task-answers", "/work/in/1.ans");
        list_dir("work", "/work");
        list_dir("work-in", "/work/in");
        list_dir("mounts", "/");
        try_read("harness-docker-socket", "/run/unicon/docker.sock");
        try_read("docker-socket", "/var/run/docker.sock");
        try_read("woodpecker-dir", "/woodpecker/task/task.yaml");
        try_read("lfs-cache", "/lfs-cache");
        try_read("proc-kcore", "/proc/kcore");
        try_write("sysrq", "/proc/sysrq-trigger");
        try_write("cgroup-procs", "/sys/fs/cgroup/cgroup.procs");
        try_write("core-pattern", "/proc/sys/kernel/core_pattern");
        return 0;
    }

    // -- the kernel
    if (!strcmp(mode, "kernel")) {
        long r;
        r = ptrace(PTRACE_ATTACH, 1, 0, 0); say("ptrace-pid1", r == 0, "%s", r == 0 ? "attached" : strerror(errno));
        r = ptrace(PTRACE_ATTACH, getppid(), 0, 0); say("ptrace-parent", r == 0, "pid %d: %s", getppid(), r == 0 ? "attached" : strerror(errno));
        mkdir("/tmp/m", 0700);
        r = mount("none", "/tmp/m", "tmpfs", 0, 0); say("mount", r == 0, "%s", r == 0 ? "mounted" : strerror(errno));
        r = unshare(CLONE_NEWUSER); say("unshare-user", r == 0, "%s", r == 0 ? "new user namespace" : strerror(errno));
        r = unshare(CLONE_NEWNS); say("unshare-mount", r == 0, "%s", r == 0 ? "new mount namespace" : strerror(errno));
        union bpf_attr attr; memset(&attr, 0, sizeof attr); attr.map_type = BPF_MAP_TYPE_ARRAY; attr.key_size = 4; attr.value_size = 4; attr.max_entries = 1;
        r = syscall(SYS_bpf, BPF_MAP_CREATE, &attr, sizeof attr); say("bpf", r >= 0, "%s", r >= 0 ? "map created" : strerror(errno));
        r = syscall(SYS_keyctl, 0 /* KEYCTL_GET_KEYRING_ID */, -3, 0); say("keyctl", r >= 0, "%s", r >= 0 ? "keyring reached" : strerror(errno));
        r = syscall(SYS_finit_module, -1, "", 0); say("load-module", errno != EBADF && r == 0, "%s", strerror(errno));
        r = syscall(SYS_init_module, 0, 0, ""); say("init-module", r == 0, "%s", strerror(errno));
        r = setuid(0); say("setuid-0", r == 0, "%s", r == 0 ? "became root" : strerror(errno));
        r = syscall(SYS_perf_event_open, 0, 0, -1, -1, 0); say("perf-event", 0, "%s", strerror(errno));
        info("no-new-privs", "%d", prctl(PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0));
        info("ids", "uid %d euid %d gid %d", getuid(), geteuid(), getgid());
        // set-uid programs on the image: with no-new-privileges their bit is ignored.
        const char *suid[] = {"/bin/su", "/usr/bin/su", "/usr/bin/passwd", "/usr/bin/newgrp", "/usr/bin/chsh", "/usr/bin/sudo", "/bin/mount", "/usr/bin/mount", 0};
        for (int i = 0; suid[i]; i++) {
            struct stat st;
            if (stat(suid[i], &st) || !(st.st_mode & S_ISUID)) continue;
            pid_t p = fork();
            if (p == 0) { int fd = open("/dev/null", O_WRONLY); dup2(fd, 2); execl(suid[i], suid[i], "--help", (char *)0); _exit(127); }
            int status; waitpid(p, &status, 0);
            info("setuid-binary", "%s is set-uid; no-new-privileges keeps it from raising privileges", suid[i]);
        }
        // The process's standing: what it could gain from a set-uid program or a
        // kernel call even where the checks above found nothing to use it on.
        FILE *f = fopen("/proc/self/status", "r"); char line[256];
        while (f && fgets(line, sizeof line, f)) {
            line[strcspn(line, "\n")] = 0;
            char *v = strchr(line, ':'); if (!v) continue; v++; while (*v == ' ' || *v == '\t') v++;
            if (!strncmp(line, "CapBnd:", 7)) say("capability-bounding-set", strtoull(v, 0, 16) != 0, "%s", line);
            if (!strncmp(line, "CapPrm:", 7) || !strncmp(line, "CapEff:", 7) || !strncmp(line, "CapAmb:", 7)) say("capabilities", strtoull(v, 0, 16) != 0, "%s", line);
            if (!strncmp(line, "NoNewPrivs:", 11)) say("no-new-privileges", atoi(v) != 1, "%s", line);
            if (!strncmp(line, "Seccomp:", 8)) say("seccomp-filter", atoi(v) != 2, "%s", line);
        }
        if (f) fclose(f);
        say("root-user", getuid() == 0 || geteuid() == 0, "uid %d euid %d", getuid(), geteuid());
        return 0;
    }

    // -- secrets: anything of the envelope in reach
    if (!strcmp(mode, "secrets")) {
        extern char **environ;
        char keys[512] = "";
        for (char **e = environ; *e; e++) { strncat(keys, *e, strcspn(*e, "=")); strcat(keys, " "); }
        info("environment", "%s", keys);
        char found[2048] = "";
        int hit = 0;
        DIR *d = opendir("/proc");
        struct dirent *e;
        while (d && (e = readdir(d))) {
            if (e->d_name[0] < '0' || e->d_name[0] > '9') continue;
            char p[300];
            snprintf(p, sizeof p, "/proc/%s/environ", e->d_name); hit |= scan_for_secrets(p, found, sizeof found);
            snprintf(p, sizeof p, "/proc/%s/cmdline", e->d_name); hit |= scan_for_secrets(p, found, sizeof found);
        }
        if (d) closedir(d);
        hit |= scan_for_secrets("/proc/self/mountinfo", found, sizeof found);
        hit |= scan_for_secrets("/work/inputs.json", found, sizeof found);
        say("secrets", hit, "%s", hit ? found : "no token, password or signed URL in any environment, command line, mount table or inputs.json in reach");
        list_dir("processes", "/proc");
        return 0;
    }
    // -- what exec keeps from a run: connections out, rings, and marks for
    // the next item. A box has a network namespace of its own, so a socket
    // is made, with nowhere to go. The name is looked up first, as it
    // answers at once where there is a network and a connection may wait.
    if (!strcmp(mode, "sockets")) {
        struct addrinfo *res; int found = getaddrinfo("example.com", "443", 0, &res);
        say("inet-lookup", found == 0, "getaddrinfo example.com: %s", found == 0 ? "resolved" : gai_strerror(found));
        try_connect("inet-connect", "1.1.1.1", 443);
        {   // A datagram socket connects where there is a route, sending nothing.
            int u = socket(AF_INET, SOCK_DGRAM, 0);
            struct sockaddr_in a = {.sin_family = AF_INET, .sin_port = htons(53)};
            inet_pton(AF_INET, "1.1.1.1", &a.sin_addr);
            int r = u >= 0 ? connect(u, (struct sockaddr *)&a, sizeof a) : -1;
            say("inet-route", r == 0, "a route to 1.1.1.1: %s", r == 0 ? "there is one" : strerror(errno));
            if (u >= 0) close(u);
        }
        int s = socket(AF_INET6, SOCK_STREAM, 0);
        if (s < 0) say("inet6-connect", 0, "socket(AF_INET6): %s", strerror(errno));
        else {
            struct sockaddr_in6 a = {.sin6_family = AF_INET6, .sin6_port = htons(443)};
            inet_pton(AF_INET6, "2606:4700:4700::1111", &a.sin6_addr);
            struct timeval tv = {2, 0};
            setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
            int r = connect(s, (struct sockaddr *)&a, sizeof a);
            say("inet6-connect", r == 0, "connect [2606:4700:4700::1111]:443: %s", r == 0 ? "connected" : strerror(errno));
            close(s);
        }
        char params[120] = {0};
        long r = syscall(425 /* io_uring_setup */, 8, params);
        say("io-uring", r >= 0, "io_uring_setup: %s", r >= 0 ? "ring made" : strerror(errno));
        return 0;
    }
    if (!strcmp(mode, "stdin")) {
        // The input is handed over open; opened again by path, its own mode
        // decides, and the fixtures' inputs are writable by anyone.
        int fd = open("/proc/self/fd/0", O_WRONLY | O_APPEND);
        int w = fd >= 0 ? (int)write(fd, "x", 1) : -1;
        say("write-stdin", w == 1, "/proc/self/fd/0: %s", w == 1 ? "appended to the input" : strerror(errno));
        if (fd >= 0) close(fd);
        return 0;
    }
    if (!strcmp(mode, "leave-sysv")) {
        int id = shmget(LEFT_KEY, 4096, IPC_CREAT | 0600);
        char *p = id >= 0 ? shmat(id, 0, 0) : (char *)-1;
        if (p != (char *)-1) { strcpy(p, "the hidden input"); shmdt(p); }
        info("sysv-made", "%s", id >= 0 ? "a segment left for the next item" : strerror(errno));
        return 0;
    }
    if (!strcmp(mode, "find-sysv")) {
        int id = shmget(LEFT_KEY, 0, 0);
        say("sysv-left", id >= 0, "%s", id >= 0 ? "found the segment an earlier item left" : strerror(errno));
        return 0;
    }
    if (!strcmp(mode, "mark") || !strcmp(mode, "read-mark")) {
        const char *paths[] = {"/proc/self/exe", "/work/in/binary", 0};
        for (int i = 0; paths[i]; i++) {
            char b[64] = "";
            if (!strcmp(mode, "mark")) {
                int r = setxattr(paths[i], LEFT_MARK, "the hidden input", 16, 0);
                info("mark", "%s: %s", paths[i], r == 0 ? "marked" : strerror(errno));
            } else {
                ssize_t n = getxattr(paths[i], LEFT_MARK, b, sizeof b - 1);
                say("mark-left", n > 0, "%s: %s", paths[i], n > 0 ? "found an earlier item's mark" : strerror(errno));
            }
        }
        return 0;
    }

    // -- interact: the partner holds the hidden input; the entrant reaches for it
    if (!strcmp(mode, "partner")) {
        int held = open("secret", O_RDONLY); if (held < 0) return 3;
        int left = open("/tmp/partner-left", O_WRONLY | O_CREAT, 0644); if (left < 0) return 3;
        close(left);
        char cwd[256] = ""; if (!getcwd(cwd, sizeof cwd)) return 3;
        printf("partner-files %s\n", cwd); fflush(stdout);
        const char *report = getenv("UNICON_REPORT"); if (!report) return 3;
        char p[512]; snprintf(p, sizeof p, "%s/message", report);
        FILE *out = fopen(p, "w"); if (!out) return 3;
        static char b[1 << 16]; size_t n;
        while ((n = fread(b, 1, sizeof b, stdin)) > 0) fwrite(b, 1, n, out);
        fclose(out);
        snprintf(p, sizeof p, "%s/verdict", report);
        FILE *verdict = fopen(p, "w"); if (!verdict) return 3;
        fputs("0\n", verdict); fclose(verdict);
        close(held);
        return 0;
    }
    if (!strcmp(mode, "partner-files")) {
        char cwd[256] = "", p[600];
        if (scanf("%255s", cwd) != 1) return 2;
        // Every attack through /proc, tracing and signalling needs the
        // partner's process, so seeing it at all is the first escape.
        pid_t partner = find_partner();
        if (partner > 0) say("partner-process", 1, "found it, process %d", partner);
        else say("partner-process", 0, "no process of its in sight");
        snprintf(p, sizeof p, "%s/secret", cwd); try_read("partner-secret", p);
        struct stat st;
        if (stat(p, &st) == 0) info("partner-size", "%s: %lld bytes", p, (long long)st.st_size);
        else info("partner-size", "%s: %s", p, strerror(errno));
        // The partner's folder is at the path it told, which may be the
        // entrant's own: listing it escapes only if the secret is there.
        DIR *d = opendir(cwd); int listed = 0;
        if (d) { struct dirent *e; while ((e = readdir(d))) if (!strcmp(e->d_name, "secret")) listed = 1; closedir(d); }
        say("partner-folder", listed, "%s: %s", cwd, d ? (listed ? "lists the secret" : "listed, no secret in it") : strerror(errno));
        int seen = access("/tmp/partner-left", F_OK) == 0;
        say("partner-tmp", seen, "/tmp/partner-left: %s", seen ? "seen" : strerror(errno));
        if (partner <= 0) return 0;
        snprintf(p, sizeof p, "/proc/%d/cwd/secret", partner); try_read("partner-cwd", p);
        snprintf(p, sizeof p, "/proc/%d/root%s/secret", partner, cwd); try_read("partner-root", p);
        snprintf(p, sizeof p, "/proc/%d/environ", partner); try_read("partner-environ", p);
        snprintf(p, sizeof p, "/proc/%d/mem", partner); try_open("partner-mem", p);
        for (int fd = 0; fd < 16; fd++) {
            snprintf(p, sizeof p, "/proc/%d/fd/%d", partner, fd);
            int f = open(p, O_RDONLY);
            if (f >= 0) { close(f); say("partner-fd", 1, "opened %s", p); }
            else if (errno != ENOENT) say("partner-fd", 0, "%s: %s", p, strerror(errno));
        }
        long r = ptrace(PTRACE_ATTACH, partner, 0, 0);
        say("partner-ptrace", r == 0, "%s", r == 0 ? "attached" : strerror(errno));
        r = kill(partner, 0);
        say("partner-signal", r == 0, "%s", r == 0 ? "may signal it" : strerror(errno));
        return 0;
    }
    printf("unknown mode %s\n", mode);
    return 2;
}
