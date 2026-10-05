// The escape fixtures: one C program, built by the compile primitive as a
// contestant's program would be, that tries the attack named on its standard
// input and prints one line per thing it tried,
// "<check> REFUSED|ESCAPED|INFO <detail>". harness/tests/test_escape.py runs
// it through sandbox-run, as the container's own process under the harness's
// flags, and with each of those flags taken away in turn.
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
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

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

static void list_dir(const char *check, const char *path) {
    DIR *d = opendir(path);
    if (!d) { info(check, "%s: %s", path, strerror(errno)); return; }
    char names[900] = ""; struct dirent *e;
    while ((e = readdir(d))) if (e->d_name[0] != '.' && strlen(names) < 800) { strcat(names, e->d_name); strcat(names, " "); }
    closedir(d);
    info(check, "%s holds: %s", path, names);
}

static void *spin(void *a) { pause(); return a; }

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

int main(void) {
    char mode[64] = "";
    if (scanf("%63s", mode) != 1) return 2;
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
        // Any interface but loopback is a network to use, whether or not this
        // machine can reach the internet through it.
        DIR *d = opendir("/sys/class/net");
        char names[512] = ""; struct dirent *e; int others = 0;
        while (d && (e = readdir(d))) {
            if (e->d_name[0] == '.') continue;
            if (strlen(names) < 400) { strcat(names, e->d_name); strcat(names, " "); }
            if (strcmp(e->d_name, "lo")) others++;
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
    printf("unknown mode %s\n", mode);
    return 2;
}
