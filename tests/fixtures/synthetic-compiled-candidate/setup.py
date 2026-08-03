from setuptools import Extension, setup

setup(
    name="moseq2-extract",
    version="9.0.0",
    ext_modules=[
        Extension(
            "moseq2_test_compiled",
            ["moseq2_test_compiled.cpp"],
            language="c++",
        )
    ],
)
