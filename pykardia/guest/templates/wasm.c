/* SPDX-License-Identifier: MIT OR Apache-2.0 */
/* CPython embedding behind the original bare-WASM guest ABI. */
#define PY_SSIZE_T_CLEAN
#include "Python.h"
#include <string.h>
#include "frozen.h"

#define IMPORT(name) __attribute__((import_module("cordis"), import_name(#name)))
IMPORT(read) extern void host_read(char *);
IMPORT(write) extern void host_write(const char *, int);
IMPORT(log) extern void host_log(int, const char *, int);
IMPORT(provide) extern int host_provide(const char *, int, const char *, int);
IMPORT(view) extern int host_view(const char *, int);
IMPORT(invoke) extern int host_invoke(const char *, int, const char *, int, const char *, int);

static PyObject *raw_read(PyObject *self, PyObject *args) {
    Py_ssize_t length;
    if (!PyArg_ParseTuple(args, "n", &length)) return NULL;
    PyObject *result = PyBytes_FromStringAndSize(NULL, length);
    if (result && length) host_read(PyBytes_AS_STRING(result));
    return result;
}
static PyObject *raw_write(PyObject *self, PyObject *args) {
    const char *data; Py_ssize_t length;
    if (!PyArg_ParseTuple(args, "y#", &data, &length)) return NULL;
    host_write(data, (int)length);
    Py_RETURN_NONE;
}
static PyObject *raw_log(PyObject *self, PyObject *args) {
    int level; const char *data; Py_ssize_t length;
    if (!PyArg_ParseTuple(args, "iy#", &level, &data, &length)) return NULL;
    host_log(level, data, (int)length);
    Py_RETURN_NONE;
}
static PyObject *raw_provide(PyObject *self, PyObject *args) {
    const char *key, *value; Py_ssize_t kl, vl;
    if (!PyArg_ParseTuple(args, "y#y#", &key, &kl, &value, &vl)) return NULL;
    return PyLong_FromLong(host_provide(key, (int)kl, value, (int)vl));
}
static PyObject *raw_view(PyObject *self, PyObject *args) {
    const char *key; Py_ssize_t kl;
    if (!PyArg_ParseTuple(args, "y#", &key, &kl)) return NULL;
    return PyLong_FromLong(host_view(key, (int)kl));
}
static PyObject *raw_invoke(PyObject *self, PyObject *args) {
    const char *key, *method, *data; Py_ssize_t kl, ml, dl;
    if (!PyArg_ParseTuple(args, "y#y#y#", &key, &kl, &method, &ml, &data, &dl)) return NULL;
    return PyLong_FromLong(host_invoke(key, (int)kl, method, (int)ml, data, (int)dl));
}
static PyMethodDef methods[] = {
    {"read", raw_read, METH_VARARGS, "Read the current host message."},
    {"write", raw_write, METH_VARARGS, "Write output to the host."},
    {"log", raw_log, METH_VARARGS, "Log through the host."},
    {"provide", raw_provide, METH_VARARGS, "Provide a service."},
    {"view", raw_view, METH_VARARGS, "View a service."},
    {"invoke", raw_invoke, METH_VARARGS, "Invoke a service."},
    {NULL, NULL, 0, NULL}
};
static struct PyModuleDef abi_module = {
    PyModuleDef_HEAD_INIT, "_pykardia_abi", NULL, -1, methods
};
static PyObject *abi_init(void) { return PyModule_Create(&abi_module); }
static PyObject *dispatcher;

static int report_error(void) {
    PyObject *type, *value, *traceback;
    PyErr_Fetch(&type, &value, &traceback);
    PyObject *text = value ? PyObject_Str(value) : NULL;
    const char *message = text ? PyUnicode_AsUTF8(text) : NULL;
    if (!message) message = "Python guest initialization failed";
    host_write(message, (int)strlen(message));
    Py_XDECREF(text); Py_XDECREF(type); Py_XDECREF(value); Py_XDECREF(traceback);
    return 1;
}
static int initialize(void) {
    if (dispatcher) return 0;
    if (!Py_IsInitialized()) {
        PyImport_FrozenModules = modules;
        if (PyImport_AppendInittab("_pykardia_abi", abi_init) < 0) return 1;
        PyConfig config;
        PyConfig_InitIsolatedConfig(&config);
        config.site_import = 0;
        config.use_hash_seed = 1;
        config.hash_seed = 0;
        config.module_search_paths_set = 1;
        config.buffered_stdio = 0;
        PyConfig_SetString(&config, &config.filesystem_encoding, L"utf-8");
        PyConfig_SetString(&config, &config.filesystem_errors, L"surrogateescape");
        PyConfig_SetString(&config, &config.stdio_encoding, L"utf-8");
        PyStatus status = Py_InitializeFromConfig(&config);
        PyConfig_Clear(&config);
        if (PyStatus_Exception(status)) {
            const char *message = status.err_msg ? status.err_msg : "initialization failed";
            host_write(message, (int)strlen(message));
            return 1;
        }
    }
    PyObject *entry = PyImport_ImportModule("_pykardia_entry");
    if (!entry) return report_error();
    dispatcher = PyObject_GetAttrString(entry, "dispatch");
    Py_DECREF(entry);
    return dispatcher ? 0 : report_error();
}
static int dispatch(const char *name, int length) {
    if (initialize()) return 1;
    PyObject *input = PyBytes_FromStringAndSize(NULL, length);
    if (!input) return report_error();
    if (length) host_read(PyBytes_AS_STRING(input));
    PyObject *result = PyObject_CallFunction(dispatcher, "sO", name, input);
    Py_DECREF(input);
    if (!result) return report_error();
    int status = (int)PyLong_AsLong(PyTuple_GET_ITEM(result, 0));
    PyObject *output = PyTuple_GET_ITEM(result, 1);
    if (output != Py_None) host_write(PyBytes_AS_STRING(output), (int)PyBytes_GET_SIZE(output));
    Py_DECREF(result);
    return status;
}
__attribute__((export_name("cordis_meta")))
int cordis_meta(void) { return dispatch("meta", 0); }
__attribute__((export_name("cordis_apply")))
int cordis_apply(int length) { return dispatch("apply", length); }
__attribute__((export_name("cordis_call")))
int cordis_call(int length) { return dispatch("call", length); }
__attribute__((export_name("cordis_dispose")))
void cordis_dispose(void) { (void)dispatch("dispose", 0); }
