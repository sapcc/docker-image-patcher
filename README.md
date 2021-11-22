# Docker Image Patcher
The Docker Image Patcher with its command `docker-image-patch` is a tool to take an existing
Docker image, apply a patchset onto it and then rebuild a new image off of this. Patches can
either be supplied in `.patch` file format or can be generated from a git repository.

## Installation
As usual, python3 required, virtualenv recommended. Make sure your pip is a python3-pip.

```shell
$ pip install git+https://github.com/sapcc/docker-image-patcher
```

## Usage
You need at least:
 * the base Docker image that should be patched (`--base-image`)
 * a repository path aka new image name (`--repository`)
 * at least one patch, either as file (`--patch`) or from a git (`--git`)

You can also add:
 * a list of new tags for the new Docker image (`--tags`)

A patch is defined by a source and a `docker-workdir`. The `docker-workdir` is the path inside
the Docker image where the patch can be applied with `git apply` (similar to `patch -p1`). This is
generally the path inside the image where the application is installed that is about to be patched.

With `--git` a patch can be automatically generated from a local git repository. This option takes
one to three arguments in the format of `[[path/to/git] git-ref] <docker-workdir>]`. `path/to/git`
refers to the path to the git repo and defaults to `.`. `git-ref` can be any git reference, e.g. a
commit hash or a range, which will then be given to `git diff` to create the patch. The defaul is
`HEAD`, which will result in a patch with all uncommited changes.

`--patch` takes a list of patches that will be applied. Multiple patches can be specified for each
`--patch`.

`--git` and `--patch` can be used multiple times. The order in which they are supplied matters, as
this is also the order the patches are applied in.

Other convenience functions include running commands inside the image via `-c / --run-before` or
`--run-after` and copying files or directories into the image via `--copy`.

## Examples
Add patch `blubb.patch` to image `foo:latest`, resulting in an image `bar:special-fix`:
```shell
$ docker-image-patch -b foo:latest -r bar -t special-fix -p blubb.patch /var/lib/my-app/
```

Add uncommited changes in local git to image:
```shell
$ docker-image-patch -b foo:latest -r bar -t special-fix -g /var/lib/my-app/
```

Add a set of commits from the current repository:
```shell
$ docker-image-patch -b foo:latest -r bar -t special-fix -g ef69b187..58a94380 /var/lib/my-app/
```

Add a set of commits from another repository and a set of patches:
```shell
$ docker-image-patch -b foo:latest -r bar -t special-fix -g ~/repos/my-cool-repo/ ef69b187..58a94380 /var/lib/my-app/ -p patches/*.patch /var/lib/my-app/
```
