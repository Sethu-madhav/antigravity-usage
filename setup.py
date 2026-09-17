from setuptools import setup, find_packages

setup(
    name="antigravity-usage",
    version="0.1.0",
    packages=find_packages(),
    entry_points={
        "console_scripts": [
            "agy-usage=agy_usage.cli:main",
        ],
    },
)
