#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <numeric>
#include <vector>

static PyObject* answer(PyObject*, PyObject*) {
    const std::vector<long> values{20, 22};
    return PyLong_FromLong(std::accumulate(values.begin(), values.end(), 0L));
}

static PyMethodDef methods[] = {
    {"answer", answer, METH_NOARGS, "Return the compiled smoke-test value."},
    {nullptr, nullptr, 0, nullptr},
};

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT,
    "moseq2_test_compiled",
    nullptr,
    -1,
    methods,
};

PyMODINIT_FUNC PyInit_moseq2_test_compiled(void) {
    return PyModule_Create(&module);
}
