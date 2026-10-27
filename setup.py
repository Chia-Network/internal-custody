#!/usr/bin/env python

from setuptools import setup, find_packages

with open("README.md", "rt") as fh:
    long_description = fh.read()

dependencies = [
    "chia-blockchain==2.7.5",
    "click==8.5.0",
    "segno==1.4.1",
    "hsms==0.3.2",
]

dev_dependencies = [
    "flake8",
    "mypy",
    "black==26.10.1",
    "pytest",
    "pytest-asyncio",
    "pytest-env",
]

setup(
    name="chia-internal-custody",
    packages=find_packages(exclude=("tests",)),
    author="Quexington",
    entry_points={
        "console_scripts": ["cic = cic.cli.main:main"],
    },
    package_data={
        "": ["*.clvm.hex", "*.clsp.hex"],
    },
    author_email="m.hauff@chia.net",
    setup_requires=["setuptools_scm"],
    install_requires=dependencies,
    url="https://github.com/Chia-Network/internal-custody",
    license="https://opensource.org/licenses/Apache-2.0",
    description="Custody puzzles and CLI tailored to Chia Network's business and security requirements",
    long_description=long_description,
    long_description_content_type="text/markdown",
    python_requires=">=3.10",
    classifiers=[
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "License :: OSI Approved :: Apache Software License",
        "Topic :: Security :: Cryptography",
    ],
    extras_require=dict(
        dev=dev_dependencies,
    ),
    project_urls={
        "Bug Reports": "https://github.com/Chia-Network/internal-custody",
        "Source": "https://github.com/Chia-Network/internal-custody",
    },
)
