"""Compatibility entry point for the paper baseline's legacy pip."""

from setuptools import find_packages, setup


setup(
    name="windtower-autofem",
    version="0.2.0",
    package_dir={"": "core"},
    packages=find_packages("core", include=["windtower_backend", "windtower_backend.*"]),
)
