/* Feasibility fixture, not the production guest SDK adapter. */
#include "Python.h"
#include <stdlib.h>
#include <string.h>
#include "frozen.h"
#include "guest_source.h"

__attribute__((import_module("cordis"), import_name("read")))
extern void host_read(char *);
__attribute__((import_module("cordis"), import_name("write")))
extern void host_write(const char *, int);
__attribute__((import_module("cordis"), import_name("log")))
extern void host_log(int, const char *, int);
__attribute__((import_module("cordis"), import_name("provide")))
extern int host_provide(const char *, int, const char *, int);

static PyObject *globals;

static int report_error(void) {
    PyObject *type, *value, *traceback;
    PyErr_Fetch(&type, &value, &traceback);
    PyObject *text = value ? PyObject_Str(value) : NULL;
    const char *message = text ? PyUnicode_AsUTF8(text) : "Python guest initialization failed";
    host_write(message, strlen(message));
    Py_XDECREF(text);
    Py_XDECREF(type);
    Py_XDECREF(value);
    Py_XDECREF(traceback);
    return 1;
}

static int initialize(void) {
    if (globals) return 0;
    PyImport_FrozenModules = modules;
    PyConfig config;
    PyConfig_InitIsolatedConfig(&config);
    config.site_import = 0;
    config.use_hash_seed = 1;
    config.hash_seed = 0;
    config.module_search_paths_set = 1;
    PyConfig_SetString(&config, &config.filesystem_encoding, L"utf-8");
    PyConfig_SetString(&config, &config.filesystem_errors, L"surrogateescape");
    PyStatus status = Py_InitializeFromConfig(&config);
    PyConfig_Clear(&config);
    if (PyStatus_Exception(status)) {
        const char *message = status.err_msg ? status.err_msg : "initialization failed";
        host_write(message, strlen(message));
        return 1;
    }
    globals = PyDict_New();
    PyDict_SetItemString(globals, "__builtins__", PyEval_GetBuiltins());
    PyObject *result = PyRun_String(guest_source, Py_file_input, globals, globals);
    if (!result) return report_error();
    Py_DECREF(result);
    return 0;
}

static int dispatch(const char *name, int length, int output_kind) {
    if (initialize()) return 1;
    PyObject *input = PyBytes_FromStringAndSize(NULL, length);
    if (!input) return report_error();
    if (length) host_read(PyBytes_AS_STRING(input));
    PyObject *result = PyObject_CallOneArg(PyDict_GetItemString(globals, name), input);
    Py_DECREF(input);
    if (!result) return report_error();
    char *data = PyBytes_AS_STRING(result);
    int size = (int)PyBytes_GET_SIZE(result);
    int status = 0;
    if (output_kind == 1) {
        status = host_provide("counter", 7, data, size);
        if (status < 0) {
            int error_size = -1 - status;
            char *error = malloc(error_size);
            host_read(error);
            host_write(error, error_size);
            free(error);
            status = 1;
        } else status = 0;
    } else if (output_kind == 2) {
        host_log(2, data, size);
    } else host_write(data, size);
    Py_DECREF(result);
    return status;
}

__attribute__((export_name("cordis_meta")))
int cordis_meta(void) { return dispatch("meta", 0, 0); }
__attribute__((export_name("cordis_apply")))
int cordis_apply(int length) { return dispatch("apply", length, 1); }
__attribute__((export_name("cordis_call")))
int cordis_call(int length) { return dispatch("call", length, 0); }
__attribute__((export_name("cordis_dispose")))
void cordis_dispose(void) { (void)dispatch("dispose", 0, 2); }
