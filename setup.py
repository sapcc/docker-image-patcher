#!/usr/bin/env python

from distutils.core import setup

setup(
    name='docker-image-patcher',
    version='0.2.0',
    description='',
    author='Sebastian Lohff',
    author_email='sebastian.lohff@sap.com',
    url='https://github.com/sapcc/docker-image-patcher',
    python_requires='>=3.5',
    packages=['docker_image_patcher'],
    install_requires=['fs', 'docker>=7.1.0', 'requests>=2.32.0'],
    classifiers=[
        'Programming Language :: Python :: 3',
        'Environment :: Console',
        'License :: OSI Approved :: Apache Software License',
    ],
    entry_points={
        'console_scripts': [
            'docker-image-patch = docker_image_patcher.docker_image_patch:main'
        ]
    },
)
