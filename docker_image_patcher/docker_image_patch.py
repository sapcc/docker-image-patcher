#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import datetime
import docker
import os
import subprocess
import sys

import fs.tempfs


def _parser():
    parser = argparse.ArgumentParser()

    # docker file
    parser.add_argument('-b', '--base-image', required=True, help='Image to base the patched image onto')
    parser.add_argument('-r', '--repository', required=False,
                        help='Image name / target docker repo (base image repo is used when not specified)')
    parser.add_argument('-t', '--tags', nargs='+', default=[], help='Additional tags to add to the image')
    parser.add_argument('--tag-time', default=None, action="store_true",
                        help="Tag image with current time (default if no tags are specified)")
    parser.add_argument('-w', '--docker-workdir', default=None,
                        help='Workdir to set in the final image, defaults to workdir of base image')
    parser.add_argument('--docker-user', default=None,
                        help='User to set in the final image, defaults to user of base image')

    # patches
    parser.add_argument('-g', '--git', metavar='[[path/to/git] git-ref] <docker-workdir>]',
                        nargs='+', action='append', default=[],
                        help='Generate a patch from git. Has 1-3 arguments. The first (optional) argument is '
                             'the path to the git, defaults to cwd. The second (optional) is the git-ref, e.g. '
                             'a commit hash, defaults to HEAD. The third (required) argument is the path '
                             'insode the docker image where the patch command will be executed.')
    parser.add_argument('-p', '--patch', metavar='<path/to/patch> [path/to/patch ...] <docker-workdir>',
                        nargs='+', action='append', default=[],
                        help='Similar to --git, but uses a pregenerated patch file')

    # other
    parser.add_argument('-q', '--quiet', default=False, action='store_true', help='Be a little more quiet')

    return parser


def main():
    parser = _parser()
    args = parser.parse_args()

    # verify correct amount of arguments for --git and --patch
    for arg in args.git:
        if len(arg) > 3:
            parser.error('Wrong argument count for --git - must be <= 3 (for argument {})'
                         ''.format(arg))

    for arg in args.patch:
        if len(arg) < 2:
            parser.error('Wrong argument count for --patch - must be >= 2 (for argument {})'
                         ''.format(arg))

    # check if we're given any patches
    if not args.git and not args.patch:
        parser.error("Neither --git nor --patch specified")

    if ':' not in args.base_image:
        parser.error("Please specify a tag for the base image")

    if not args.repository:
        args.repository = "".join(args.base_image.split(":")[:-1])

    # as the order of the patches is quite important, we need to look
    # into argv for the order of --patch / --git commands
    git_flags = ('-g', '--git')
    patch_flags = ('-p', '--patch')
    git_patch_order = []
    for arg in sys.argv:
        if arg in git_flags:
            git_patch_order.append('git')
        elif arg in patch_flags:
            git_patch_order.append('patch')

    # create docker filesystem
    dockerfs = fs.tempfs.TempFS("docker-live-patch", auto_clean=True)

    # generate patchset
    def add_patch(patch_count, name, diff):
        patch_path = "{:04d}-{}{}".format(patch_count, name, '' if name.endswith('.patch') else '.patch')
        dockerfs.settext('/' + patch_path, diff)
        patches.append((patch_path, opt[-1]))

    patches = []
    patch_count = 0
    for n, opt_type in enumerate(git_patch_order):
        if opt_type == 'git':
            opt = args.git.pop(0)
            git_path, git_ref = '.', 'HEAD'
            if len(opt) == 2:
                git_ref = opt[0]
            elif len(opt) == 3:
                git_path, git_ref = opt[0:2]

            name = git_ref
            name = name.replace("/", "_")
            if '..' not in name:
                name += '-HEAD+staged'

            try:
                diff = subprocess.check_output(['git', '-C', git_path, 'diff', git_ref]).decode()
            except subprocess.CalledProcessError as e:
                print('Error: Could not acquire git diff for git "{}" ({}) - is the git path correct?'
                      ''.format(git_path, e),
                      file=sys.stderr)
                sys.exit(1)

            if not diff.strip():
                print('Error: Diff for git "{}" ref {} is empty!'.format(git_path, git_ref))
                sys.exit(1)

            add_patch(patch_count, name, diff)
            patch_count += 1
        else:
            opt = args.patch.pop(0)
            for path in opt[:-1]:
                name = os.path.basename(path)
                with open(path) as f:
                    diff = f.read()
                add_patch(patch_count, name, diff)
                patch_count += 1

    # assert everything has been processed
    assert not args.git
    assert not args.patch

    # fetch original values from base image
    if not args.quiet:
        print("Pulling {} ...".format(args.base_image))
    client = docker.from_env()
    try:
        docker_base_image = client.images.pull(args.base_image)
    except docker.errors.NotFound as e:
        print("Error: Could not pull base image - {}".format(e), file=sys.stderr)
        sys.exit(1)

    orig_user = docker_base_image.attrs['Config'].get('User', '')
    orig_workdir = docker_base_image.attrs['Config'].get('WorkDir', '/')

    # write docker file
    dockerfile = []
    dockerfile.append("FROM {}".format(args.base_image))
    dockerfile.append("USER root")
    dockerfile.append("")

    for patch_name, patch_workdir in patches:
        print("Adding patch", patch_name)
        dockerfile.append('# patch {}'.format(patch_name))
        dockerfile.append('COPY "{}" /'.format(patch_name))
        dockerfile.append('WORKDIR "{}"'.format(patch_workdir))
        dockerfile.append('RUN git apply "/{}"'.format(patch_name))
        dockerfile.append('')

    workdir = args.docker_workdir or orig_workdir
    dockerfile.append('WORKDIR "{}"'.format(workdir))
    user = args.docker_user or orig_user
    if user:
        dockerfile.append('USER "{}"'.format(user))
    dockerfs.settext('/Dockerfile', '\n'.join(dockerfile))

    # build docker image
    time_tag = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    if not args.tags or (args.tag_time and time_tag not in args.tags):
        args.tags.append(time_tag)

    tags = []
    for tag in args.tags:
        tags.append("{}:{}".format(args.repository, tag))

    print("Building docker image...")
    try:
        image, log = client.images.build(path=dockerfs.getsyspath(''), tag=tags)
    except docker.errors.BuildError as e:
        print('Error: Build failed! {}'.format(e.msg), file=sys.stderr)
        print('Leaving docker filesystem intact for you to inspect in {}'
              ''.format(dockerfs.getsyspath('')), file=sys.stderr)
        sys.exit(1)

    dockerfs.close()

    if not args.quiet:
        print()
        print(" --- Docker build log ---")
        for line in log:
            if 'stream' in line:
                print(line['stream'], end='')

    # done!
    print()
    print("Success! Docker image can (maybe) be pushed using one of these commands")
    for tag in tags:
        print(" - docker push {}".format(tag))


if __name__ == '__main__':
    main()
