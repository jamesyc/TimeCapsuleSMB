#include <dlfcn.h>
#include <errno.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

typedef int (*py_bytes_main_t)(int, char **);

static int is_empty_env(const char *name) {
    const char *value = getenv(name);
    return value == NULL || value[0] == '\0';
}

static void set_if_empty(const char *name, const char *value) {
    if (is_empty_env(name)) {
        setenv(name, value, 1);
    }
}

static char *join_path(const char *left, const char *right) {
    size_t left_len = strlen(left);
    size_t right_len = strlen(right);
    int needs_slash = left_len > 0 && left[left_len - 1] != '/';
    char *result = malloc(left_len + (size_t)needs_slash + right_len + 1);
    if (result == NULL) {
        perror("malloc");
        exit(70);
    }
    memcpy(result, left, left_len);
    if (needs_slash) {
        result[left_len] = '/';
    }
    memcpy(result + left_len + (size_t)needs_slash, right, right_len);
    result[left_len + (size_t)needs_slash + right_len] = '\0';
    return result;
}

static char *parent_dir(const char *path) {
    char *copy = strdup(path);
    if (copy == NULL) {
        perror("strdup");
        exit(70);
    }
    char *slash = strrchr(copy, '/');
    if (slash == NULL) {
        strcpy(copy, ".");
        return copy;
    }
    if (slash == copy) {
        slash[1] = '\0';
        return copy;
    }
    *slash = '\0';
    return copy;
}

static char *executable_path(void) {
    char stack_buffer[PATH_MAX];
    uint32_t size = sizeof(stack_buffer);
    char *buffer = NULL;

    if (_NSGetExecutablePath(stack_buffer, &size) == 0) {
        buffer = strdup(stack_buffer);
        if (buffer == NULL) {
            perror("strdup");
            exit(70);
        }
    } else {
        buffer = malloc((size_t)size);
        if (buffer == NULL) {
            perror("malloc");
            exit(70);
        }
        if (_NSGetExecutablePath(buffer, &size) != 0) {
            fprintf(stderr, "could not resolve helper executable path\n");
            free(buffer);
            exit(70);
        }
    }

    char resolved[PATH_MAX];
    if (realpath(buffer, resolved) != NULL) {
        char *result = strdup(resolved);
        free(buffer);
        if (result == NULL) {
            perror("strdup");
            exit(70);
        }
        return result;
    }
    return buffer;
}

static void mkdir_p(const char *path) {
    if (path[0] == '\0') {
        return;
    }

    char *copy = strdup(path);
    if (copy == NULL) {
        perror("strdup");
        exit(70);
    }

    for (char *cursor = copy + 1; *cursor != '\0'; cursor++) {
        if (*cursor != '/') {
            continue;
        }
        *cursor = '\0';
        if (mkdir(copy, 0755) != 0 && errno != EEXIST) {
            fprintf(stderr, "mkdir %s failed: %s\n", copy, strerror(errno));
            free(copy);
            exit(70);
        }
        *cursor = '/';
    }

    if (mkdir(copy, 0755) != 0 && errno != EEXIST) {
        fprintf(stderr, "mkdir %s failed: %s\n", copy, strerror(errno));
        free(copy);
        exit(70);
    }
    free(copy);
}

/* Sets the bundled runtime's environment and returns the bundled Python
 * framework library to run in this process. */
static char *configure_environment(const char *contents_dir) {
    char *resources_dir = join_path(contents_dir, "Resources");
    char *python_home = join_path(resources_dir, "Python/Runtime/Python.framework/Versions/Current");
    char *python_packages = join_path(resources_dir, "Python/site-packages");
    char *ca_cert_file = join_path(python_packages, "certifi/cacert.pem");
    char *library = join_path(python_home, "Python");

    setenv("PYTHONHOME", python_home, 1);

    char *state_dir = join_path(getenv("HOME") ? getenv("HOME") : "", "Library/Application Support/TimeCapsuleSMB");
    set_if_empty("TCAPSULE_STATE_DIR", state_dir);
    mkdir_p(getenv("TCAPSULE_STATE_DIR"));

    char *default_config = join_path(getenv("TCAPSULE_STATE_DIR"), ".env");
    set_if_empty("TCAPSULE_CONFIG", default_config);

    char *distribution_root = join_path(resources_dir, "Distribution");
    set_if_empty("TCAPSULE_DISTRIBUTION_ROOT", distribution_root);

    char *tools_bin = join_path(resources_dir, "Tools/bin");
    const char *old_path = getenv("PATH");
    if (old_path == NULL || old_path[0] == '\0') {
        old_path = "/usr/bin:/bin:/usr/sbin:/sbin";
    }
    char *path_value = malloc(strlen(tools_bin) + 1 + strlen(old_path) + 1);
    if (path_value == NULL) {
        perror("malloc");
        exit(70);
    }
    sprintf(path_value, "%s:%s", tools_bin, old_path);
    setenv("PATH", path_value, 1);

    const char *old_pythonpath = getenv("PYTHONPATH");
    char *pythonpath_value = NULL;
    if (old_pythonpath != NULL && old_pythonpath[0] != '\0') {
        pythonpath_value = malloc(strlen(python_packages) + 1 + strlen(old_pythonpath) + 1);
        if (pythonpath_value == NULL) {
            perror("malloc");
            exit(70);
        }
        sprintf(pythonpath_value, "%s:%s", python_packages, old_pythonpath);
    } else {
        pythonpath_value = strdup(python_packages);
        if (pythonpath_value == NULL) {
            perror("strdup");
            exit(70);
        }
    }
    setenv("PYTHONPATH", pythonpath_value, 1);
    setenv("PYTHONNOUSERSITE", "1", 1);
    setenv("PYTHONDONTWRITEBYTECODE", "1", 1);

    if (access(ca_cert_file, F_OK) == 0) {
        set_if_empty("SSL_CERT_FILE", ca_cert_file);
        set_if_empty("REQUESTS_CA_BUNDLE", ca_cert_file);
    }

    free(resources_dir);
    free(python_home);
    free(python_packages);
    free(ca_cert_file);
    free(state_dir);
    free(default_config);
    free(distribution_root);
    free(tools_bin);
    free(path_value);
    free(pythonpath_value);
    return library;
}

int main(int argc, char **argv) {
    char *helper_path = executable_path();
    char *helpers_dir = parent_dir(helper_path);
    char *contents_dir = parent_dir(helpers_dir);
    char *library = configure_environment(contents_dir);

    char **python_argv = calloc((size_t)argc + 3, sizeof(char *));
    if (python_argv == NULL) {
        perror("calloc");
        return 70;
    }
    python_argv[0] = helper_path;
    python_argv[1] = "-m";
    python_argv[2] = "timecapsulesmb.cli.main";
    for (int i = 1; i < argc; i++) {
        python_argv[i + 2] = argv[i];
    }

    /* Run Python in this process instead of exec'ing the framework's
     * bin/python3, which re-execs Resources/Python.app. macOS gives an app
     * bundle's main executable its own Local Network permission
     * ("Python"), while a helper like this one shares the app's.
     * RTLD_GLOBAL: the framework's extension modules leave Py* symbols
     * undefined and find them in the global namespace when they load. */
    void *handle = dlopen(library, RTLD_NOW | RTLD_GLOBAL);
    if (handle == NULL) {
        fprintf(stderr, "load %s failed: %s\n", library, dlerror());
        return 70;
    }
    py_bytes_main_t py_bytes_main = (py_bytes_main_t)dlsym(handle, "Py_BytesMain");
    if (py_bytes_main == NULL) {
        fprintf(stderr, "load %s failed: %s\n", library, dlerror());
        return 70;
    }
    return py_bytes_main(argc + 2, python_argv);
}
