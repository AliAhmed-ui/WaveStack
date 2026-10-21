"""Build the native wavestack_audio module.

    ./venv/bin/pip install pybind11 setuptools
    ./venv/bin/python3 setup.py build_ext --inplace

miniaudio.h must sit next to AudioEngine.cpp.
"""
import sys

import pybind11
from setuptools import Extension, setup

if sys.platform == "win32":
    compile_args = ["/O2", "/std:c++17", "/EHsc"]
    link_args = []
    libs = []
else:
    compile_args = ["-O3", "-std=c++17", "-fvisibility=hidden"]
    link_args = []
    # miniaudio loads ALSA/Pulse dynamically; it only needs these at link time.
    libs = ["pthread", "dl", "m"]

ext = Extension(
    "wavestack_audio",
    sources=["AudioEngine.cpp"],
    include_dirs=[pybind11.get_include(), "."],
    libraries=libs,
    extra_compile_args=compile_args,
    extra_link_args=link_args,
    language="c++",
)

setup(name="wavestack_audio", version="0.1.0", ext_modules=[ext])
