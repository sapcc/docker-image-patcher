"""Thorough tests for docker_image_patch.py.

Tests are written against the existing behaviour so that they serve as a
regression safety-net when we replace the ``fs`` dependency with stdlib.
"""

import datetime
import json
import os
import pathlib
import shutil
import sys
import textwrap
from unittest import mock

import pytest

from docker_image_patcher import docker_image_patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse(argv_tail):
    """Run the argument parser on a list of args (after program name)."""
    return docker_image_patch._parser().parse_args(argv_tail)


class FakeImage:
    """Minimal stand-in for ``docker.models.images.Image``."""

    def __init__(self, attrs=None, tags=None):
        self.attrs = attrs or {'Config': {}}
        self._tags = tags or []

    def tag(self, tag):
        self._tags.append(tag)


class FakeDockerClient:
    """Minimal stand-in for ``docker.DockerClient``.

    On ``build()``, snapshots the entire build context directory so that
    tests can inspect the Dockerfile and other files *after* ``main()``
    returns (at which point ``dockerfs.clean()`` will have already
    removed them from disk).
    """

    def __init__(self, pull_image=None, build_image=None,
                 build_error=None, push_results=None):
        self._pull_image = pull_image or FakeImage()
        self._build_image = build_image or FakeImage()
        self._build_error = build_error
        self._push_results = push_results or []
        # record calls
        self.pull_calls = []
        self.build_calls = []
        self.push_calls = []
        self.images = self
        # snapshots filled during build()
        self.build_context_files = {}   # {relative_path: content_bytes_or_str}
        self.build_context_dirs = set()

    # -- images.pull
    def pull(self, image):
        self.pull_calls.append(image)
        return self._pull_image

    # -- images.build
    def build(self, **kwargs):
        self.build_calls.append(kwargs)
        # snapshot build context before returning (cleanup happens in finally)
        build_path = pathlib.Path(kwargs.get('path', ''))
        if build_path.is_dir():
            for item in build_path.rglob('*'):
                rel = str(item.relative_to(build_path))
                if item.is_file():
                    try:
                        self.build_context_files[rel] = item.read_text()
                    except UnicodeDecodeError:
                        self.build_context_files[rel] = item.read_bytes()
                elif item.is_dir():
                    self.build_context_dirs.add(rel)
        if self._build_error:
            raise self._build_error
        return self._build_image, [{'stream': 'ok\n'}]

    # -- images.push
    def push(self, tag, stream=False):
        self.push_calls.append(tag)
        return self._push_results

    # convenience accessors
    def get_dockerfile(self):
        return self.build_context_files.get('Dockerfile', '')

    def get_file(self, name):
        return self.build_context_files.get(name)

    def list_files(self, glob_pattern='*'):
        """Return file names matching a simple suffix pattern."""
        from fnmatch import fnmatch
        return sorted(k for k in self.build_context_files if fnmatch(k, glob_pattern))


def _make_client(pull_attrs=None, **kwargs):
    """Shorthand to create a FakeDockerClient with common pull_image attrs."""
    attrs = pull_attrs or {'Config': {'User': '', 'WorkDir': '/'}}
    return FakeDockerClient(
        pull_image=FakeImage(attrs=attrs), **kwargs)


def _run_main(argv, client, extra_mocks=None):
    """Run main() with mocked docker client and optional extra mocks."""
    ctx_managers = [
        mock.patch('sys.argv', argv),
        mock.patch('docker.from_env', return_value=client),
        mock.patch.object(client, 'pull', return_value=client._pull_image),
    ]
    if extra_mocks:
        ctx_managers.extend(extra_mocks)
    # nest all context managers
    with ctx_managers[0]:
        with ctx_managers[1]:
            with ctx_managers[2]:
                if len(ctx_managers) > 3:
                    with ctx_managers[3]:
                        docker_image_patch.main()
                else:
                    docker_image_patch.main()


# ---------------------------------------------------------------------------
# 1. Argument parser tests
# ---------------------------------------------------------------------------

class TestParser:
    """Tests for _parser() and its defaults/validation."""

    def test_base_image_required(self):
        with pytest.raises(SystemExit):
            _parse([])

    def test_minimal_args(self):
        args = _parse(['-b', 'img:tag', '-g', '/app'])
        assert args.base_image == 'img:tag'
        assert args.git == [['/app']]

    def test_repository_default_none(self):
        args = _parse(['-b', 'img:tag', '-g', '/app'])
        assert args.repository is None

    def test_repository_explicit(self):
        args = _parse(['-b', 'img:tag', '-r', 'myrepo', '-g', '/app'])
        assert args.repository == 'myrepo'

    def test_tags_default_empty(self):
        args = _parse(['-b', 'img:tag', '-g', '/app'])
        assert args.tags == []

    def test_tags_multiple(self):
        args = _parse(['-b', 'img:tag', '-t', 'a', 'b', '-g', '/app'])
        assert args.tags == ['a', 'b']

    def test_tag_time_flag(self):
        args = _parse(['-b', 'img:tag', '--tag-time', '-g', '/app'])
        assert args.tag_time is True

    def test_docker_workdir_default(self):
        args = _parse(['-b', 'img:tag', '-g', '/app'])
        assert args.docker_workdir is None

    def test_docker_workdir_explicit(self):
        args = _parse(['-b', 'img:tag', '-w', '/work', '-g', '/app'])
        assert args.docker_workdir == '/work'

    def test_docker_user_default(self):
        args = _parse(['-b', 'img:tag', '-g', '/app'])
        assert args.docker_user is None

    def test_docker_user_explicit(self):
        args = _parse(['-b', 'img:tag', '--docker-user', 'nobody', '-g', '/app'])
        assert args.docker_user == 'nobody'

    def test_run_before_default(self):
        args = _parse(['-b', 'img:tag', '-g', '/app'])
        assert args.run_before == []

    def test_run_before_values(self):
        args = _parse(['-b', 'img:tag', '-c', 'apt update', 'pip install x',
                        '-g', '/app'])
        assert args.run_before == ['apt update', 'pip install x']

    def test_run_after_default(self):
        args = _parse(['-b', 'img:tag', '-g', '/app'])
        assert args.run_after == []

    def test_run_after_values(self):
        args = _parse(['-b', 'img:tag', '--run-after', 'rm -rf /tmp',
                        '-g', '/app'])
        assert args.run_after == ['rm -rf /tmp']

    def test_copy_default(self):
        args = _parse(['-b', 'img:tag', '-g', '/app'])
        assert args.copy == []

    def test_copy_requires_two_args(self):
        with pytest.raises(SystemExit):
            _parse(['-b', 'img:tag', '--copy', 'only_one'])

    def test_copy_multiple(self):
        args = _parse(['-b', 'img:tag', '--copy', 'a', '/a',
                        '--copy', 'b', '/b', '-g', '/app'])
        assert args.copy == [['a', '/a'], ['b', '/b']]

    def test_git_accumulates(self):
        args = _parse(['-b', 'img:tag', '-g', '/app', '-g', 'ref', '/app2'])
        assert args.git == [['/app'], ['ref', '/app2']]

    def test_git_three_args(self):
        args = _parse(['-b', 'img:tag', '-g', '.', 'HEAD', '/app'])
        assert args.git == [['.', 'HEAD', '/app']]

    def test_patch_accumulates(self):
        args = _parse(['-b', 'img:tag', '-p', 'a.patch', '/app',
                        '-p', 'b.patch', '/app2'])
        assert args.patch == [['a.patch', '/app'], ['b.patch', '/app2']]

    def test_no_cache_default(self):
        args = _parse(['-b', 'img:tag', '-g', '/app'])
        assert args.no_cache is False

    def test_no_cache_flag(self):
        args = _parse(['-b', 'img:tag', '--no-cache', '-g', '/app'])
        assert args.no_cache is True

    def test_network_default(self):
        args = _parse(['-b', 'img:tag', '-g', '/app'])
        assert args.network is None

    def test_network_explicit(self):
        args = _parse(['-b', 'img:tag', '--network', 'host', '-g', '/app'])
        assert args.network == 'host'

    def test_push_image_default(self):
        args = _parse(['-b', 'img:tag', '-g', '/app'])
        assert args.push_image is False

    def test_push_image_flag(self):
        args = _parse(['-b', 'img:tag', '--push-image', '-g', '/app'])
        assert args.push_image is True

    def test_quiet_default(self):
        args = _parse(['-b', 'img:tag', '-g', '/app'])
        assert args.quiet is False

    def test_verbose_default(self):
        args = _parse(['-b', 'img:tag', '-g', '/app'])
        assert args.verbose is False

    def test_verbose_flag(self):
        args = _parse(['-b', 'img:tag', '-v', '-g', '/app'])
        assert args.verbose is True


# ---------------------------------------------------------------------------
# 2. Argument validation in main()
# ---------------------------------------------------------------------------

class TestArgValidation:
    """Tests for early validation logic inside main()."""

    def test_nothing_to_do(self):
        """No --git, --patch, --run-before, --run-after, --copy -> error."""
        with mock.patch('sys.argv', ['prog', '-b', 'img:tag']):
            with pytest.raises(SystemExit) as exc_info:
                docker_image_patch.main()
            assert exc_info.value.code == 2

    def test_base_image_missing_tag(self):
        """Base image without ':tag' -> error."""
        with mock.patch('sys.argv', ['prog', '-b', 'img', '-g', '/app']):
            with pytest.raises(SystemExit) as exc_info:
                docker_image_patch.main()
            assert exc_info.value.code == 2

    def test_git_too_many_args(self):
        """--git with >3 args -> error."""
        with mock.patch('sys.argv', ['prog', '-b', 'img:tag',
                                     '-g', 'a', 'b', 'c', 'd']):
            with pytest.raises(SystemExit) as exc_info:
                docker_image_patch.main()
            assert exc_info.value.code == 2

    def test_patch_too_few_args(self):
        """--patch with <2 args -> error."""
        with mock.patch('sys.argv', ['prog', '-b', 'img:tag',
                                     '-p', 'only_one']):
            with pytest.raises(SystemExit) as exc_info:
                docker_image_patch.main()
            assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# 3. Repository defaulting
# ---------------------------------------------------------------------------

class TestRepositoryDefault:
    """When --repository is not given, it derives from base image."""

    def test_repo_from_simple_image(self):
        client = _make_client()
        argv = ['prog', '-b', 'myrepo:latest', '--run-before', 'echo hi']
        _run_main(argv, client)
        assert client.build_calls[0]['tag'].startswith('myrepo:')

    def test_repo_from_registry_image(self):
        client = _make_client()
        argv = ['prog', '-b', 'registry.io/org/image:v1', '--run-before', 'echo hi']
        _run_main(argv, client)
        assert client.build_calls[0]['tag'].startswith('registry.io/org/image:')

    def test_explicit_repo_overrides(self):
        client = _make_client()
        argv = ['prog', '-b', 'myrepo:latest', '-r', 'custom',
                '--run-before', 'echo hi']
        _run_main(argv, client)
        assert client.build_calls[0]['tag'].startswith('custom:')


# ---------------------------------------------------------------------------
# 4. Dockerfile generation
# ---------------------------------------------------------------------------

class TestDockerfileGeneration:
    """Test the generated Dockerfile contents under various flag combos."""

    def test_basic_run_before(self):
        """--run-before produces RUN lines after FROM+USER root."""
        client = _make_client(
            pull_attrs={'Config': {'User': 'appuser', 'WorkDir': '/app'}})
        argv = ['prog', '-b', 'img:tag', '-c', 'echo hello']
        _run_main(argv, client)
        df = client.get_dockerfile()
        lines = df.strip().splitlines()
        assert lines[0] == 'FROM img:tag'
        assert lines[1] == 'USER root'
        assert 'RUN echo hello' in lines

    def test_run_after(self):
        """--run-after commands appear after patches section."""
        client = _make_client(
            pull_attrs={'Config': {'User': 'appuser', 'WorkDir': '/app'}})
        argv = ['prog', '-b', 'img:tag', '--run-after', 'cleanup']
        _run_main(argv, client)
        df = client.get_dockerfile()
        assert 'RUN cleanup' in df

    def test_workdir_from_base_image(self):
        """Default workdir comes from the base image config."""
        client = _make_client(
            pull_attrs={'Config': {'User': '', 'WorkDir': '/opt/myapp'}})
        argv = ['prog', '-b', 'img:tag', '-c', 'true']
        _run_main(argv, client)
        df = client.get_dockerfile()
        assert 'WORKDIR "/opt/myapp"' in df

    def test_workdir_override(self):
        """--docker-workdir overrides the base image workdir."""
        client = _make_client(
            pull_attrs={'Config': {'User': 'appuser', 'WorkDir': '/app'}})
        argv = ['prog', '-b', 'img:tag', '-w', '/custom', '-c', 'true']
        _run_main(argv, client)
        df = client.get_dockerfile()
        assert 'WORKDIR "/custom"' in df

    def test_user_from_base_image(self):
        """Default user comes from the base image config."""
        client = _make_client(
            pull_attrs={'Config': {'User': 'appuser', 'WorkDir': '/'}})
        argv = ['prog', '-b', 'img:tag', '-c', 'true']
        _run_main(argv, client)
        df = client.get_dockerfile()
        assert 'USER "appuser"' in df

    def test_user_override(self):
        """--docker-user overrides the base image user."""
        client = _make_client(
            pull_attrs={'Config': {'User': 'appuser', 'WorkDir': '/app'}})
        argv = ['prog', '-b', 'img:tag', '--docker-user', 'root', '-c', 'true']
        _run_main(argv, client)
        df = client.get_dockerfile()
        lines = df.strip().splitlines()
        user_lines = [l for l in lines if l.startswith('USER')]
        assert user_lines[-1] == 'USER "root"'

    def test_no_user_when_empty(self):
        """When base image has no user and none specified, no trailing USER."""
        client = _make_client(
            pull_attrs={'Config': {'User': '', 'WorkDir': '/'}})
        argv = ['prog', '-b', 'img:tag', '-c', 'true']
        _run_main(argv, client)
        df = client.get_dockerfile()
        lines = df.strip().splitlines()
        user_lines = [l for l in lines if l.startswith('USER')]
        # Only the initial "USER root"
        assert user_lines == ['USER root']

    def test_from_line(self):
        """FROM line references the base image exactly."""
        client = _make_client(
            pull_attrs={'Config': {'User': 'appuser', 'WorkDir': '/app'}})
        argv = ['prog', '-b', 'registry.io/org/img:v2', '-c', 'true']
        _run_main(argv, client)
        df = client.get_dockerfile()
        assert df.startswith('FROM registry.io/org/img:v2\n')

    def test_verbose_prints_dockerfile(self, capsys):
        """--verbose prints the Dockerfile to stdout."""
        client = _make_client(
            pull_attrs={'Config': {'User': 'appuser', 'WorkDir': '/app'}})
        argv = ['prog', '-b', 'img:tag', '-v', '-c', 'echo hi']
        _run_main(argv, client)
        captured = capsys.readouterr()
        assert 'BEGIN Dockerfile' in captured.out
        assert 'END Dockerfile' in captured.out

    def test_run_before_and_after_order(self):
        """run-before comes before run-after in the Dockerfile."""
        client = _make_client(
            pull_attrs={'Config': {'User': 'appuser', 'WorkDir': '/app'}})
        argv = ['prog', '-b', 'img:tag',
                '-c', 'BEFORE_CMD', '--run-after', 'AFTER_CMD']
        _run_main(argv, client)
        df = client.get_dockerfile()
        before_pos = df.index('RUN BEFORE_CMD')
        after_pos = df.index('RUN AFTER_CMD')
        assert before_pos < after_pos


# ---------------------------------------------------------------------------
# 5. Patch handling (git and file patches)
# ---------------------------------------------------------------------------

class TestPatchHandling:
    """Test git diff and file patch integration into the Dockerfile."""

    def test_git_patch_creates_patch_file(self):
        """--git creates a numbered .patch file in the build context."""
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-g', '/app']
        _run_main(argv, client, [
            mock.patch('subprocess.check_output',
                       return_value=b'diff --git a/f\n+hello\n'),
        ])
        patches = client.list_files('*.patch')
        assert len(patches) == 1
        assert patches[0] == '0000-HEAD-HEAD+staged.patch'
        assert client.get_file(patches[0]) == 'diff --git a/f\n+hello\n'

    def test_git_patch_dockerfile_entries(self):
        """--git adds COPY + WORKDIR + RUN git apply to the Dockerfile."""
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-g', '/app']
        _run_main(argv, client, [
            mock.patch('subprocess.check_output',
                       return_value=b'diff --git a/f\n+hello\n'),
        ])
        df = client.get_dockerfile()
        assert 'COPY "0000-HEAD-HEAD+staged.patch" /' in df
        assert 'WORKDIR "/app"' in df
        assert 'RUN git apply "/0000-HEAD-HEAD+staged.patch"' in df

    def test_git_with_ref(self):
        """--git with 2 args: ref + docker-workdir."""
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-g', 'abc123', '/app']
        _run_main(argv, client, [
            mock.patch('subprocess.check_output',
                       return_value=b'diff content\n'),
        ])
        patches = client.list_files('*.patch')
        assert patches[0] == '0000-abc123-HEAD+staged.patch'

    def test_git_with_path_and_ref(self):
        """--git with 3 args: git-path, ref, docker-workdir."""
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-g', '/some/repo', 'main..fix', '/app']
        _run_main(argv, client, [
            mock.patch('subprocess.check_output',
                       return_value=b'diff content\n'),
        ])
        patches = client.list_files('*.patch')
        # '..' in ref means no '-HEAD+staged' suffix
        assert patches[0] == '0000-main..fix.patch'

    def test_git_ref_slash_replaced(self):
        """Slashes in git refs are replaced with underscores."""
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-g', 'origin/main', '/app']
        _run_main(argv, client, [
            mock.patch('subprocess.check_output',
                       return_value=b'diff content\n'),
        ])
        patches = client.list_files('*.patch')
        assert 'origin_main' in patches[0]

    def test_git_empty_diff_exits(self):
        """Empty git diff -> exit with error."""
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-g', '/app']
        with pytest.raises(SystemExit) as exc_info:
            _run_main(argv, client, [
                mock.patch('subprocess.check_output', return_value=b'   \n'),
            ])
        assert exc_info.value.code == 1

    def test_git_diff_error_exits(self):
        """git diff failure -> exit with error."""
        import subprocess
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-g', '/app']
        with pytest.raises(SystemExit) as exc_info:
            _run_main(argv, client, [
                mock.patch('subprocess.check_output',
                           side_effect=subprocess.CalledProcessError(1, 'git')),
            ])
        assert exc_info.value.code == 1

    def test_file_patch(self, tmp_path):
        """--patch reads patch files and adds them to the build context."""
        patch_file = tmp_path / 'fix.patch'
        patch_file.write_text('diff content\n')
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-p', str(patch_file), '/app']
        _run_main(argv, client)
        patches = client.list_files('*.patch')
        assert len(patches) == 1
        assert 'fix.patch' in patches[0]
        assert client.get_file(patches[0]) == 'diff content\n'

    def test_multiple_file_patches(self, tmp_path):
        """--patch with multiple files creates multiple numbered patches."""
        p1 = tmp_path / 'a.patch'
        p1.write_text('diff a\n')
        p2 = tmp_path / 'b.patch'
        p2.write_text('diff b\n')
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-p', str(p1), str(p2), '/app']
        _run_main(argv, client)
        patches = client.list_files('*.patch')
        assert len(patches) == 2
        assert '0000-' in patches[0]
        assert '0001-' in patches[1]

    def test_git_and_patch_ordering(self, tmp_path):
        """When --git and --patch are mixed, their order in argv is preserved."""
        patch_file = tmp_path / 'fix.patch'
        patch_file.write_text('file patch\n')
        client = _make_client()
        # -p first, then -g
        argv = ['prog', '-b', 'img:tag',
                '-p', str(patch_file), '/app',
                '-g', '/app2']
        _run_main(argv, client, [
            mock.patch('subprocess.check_output',
                       return_value=b'git diff content\n'),
        ])
        df = client.get_dockerfile()
        # patch file should come before git patch in Dockerfile
        patch_pos = df.index('0000-fix.patch')
        git_pos = df.index('0001-HEAD-HEAD+staged.patch')
        assert patch_pos < git_pos


# ---------------------------------------------------------------------------
# 6. --copy handling
# ---------------------------------------------------------------------------

class TestCopyHandling:
    """Test --copy for files and directories."""

    def test_copy_file(self, tmp_path):
        """--copy with a file copies it into the build context."""
        src_file = tmp_path / 'myfile.txt'
        src_file.write_text('hello')
        client = _make_client()
        argv = ['prog', '-b', 'img:tag',
                '--copy', str(src_file), '/dest/myfile.txt']
        _run_main(argv, client)
        # The file should be in the build context under copy-00000000/
        key = 'copy-00000000/myfile.txt'
        assert key in client.build_context_files
        assert client.get_file(key) == 'hello'
        # Dockerfile should have COPY instruction
        df = client.get_dockerfile()
        assert 'COPY ["copy-00000000/myfile.txt", "/dest/myfile.txt"]' in df

    def test_copy_directory(self, tmp_path):
        """--copy with a directory copies the whole tree."""
        src_dir = tmp_path / 'mydir'
        src_dir.mkdir()
        (src_dir / 'a.txt').write_text('file a')
        (src_dir / 'sub').mkdir()
        (src_dir / 'sub' / 'b.txt').write_text('file b')

        client = _make_client()
        argv = ['prog', '-b', 'img:tag',
                '--copy', str(src_dir), '/dest/mydir']
        _run_main(argv, client)
        assert client.get_file('copy-00000000/mydir/a.txt') == 'file a'
        assert client.get_file('copy-00000000/mydir/sub/b.txt') == 'file b'

    def test_copy_multiple(self, tmp_path):
        """Multiple --copy flags create separate numbered directories."""
        f1 = tmp_path / 'one.txt'
        f1.write_text('1')
        f2 = tmp_path / 'two.txt'
        f2.write_text('2')

        client = _make_client()
        argv = ['prog', '-b', 'img:tag',
                '--copy', str(f1), '/a',
                '--copy', str(f2), '/b']
        _run_main(argv, client)
        assert 'copy-00000000/one.txt' in client.build_context_files
        assert 'copy-00000001/two.txt' in client.build_context_files

    def test_copy_before_patches_in_dockerfile(self, tmp_path):
        """COPY instructions appear before patch instructions."""
        src_file = tmp_path / 'f.txt'
        src_file.write_text('data')
        patch_file = tmp_path / 'fix.patch'
        patch_file.write_text('diff\n')

        client = _make_client()
        argv = ['prog', '-b', 'img:tag',
                '--copy', str(src_file), '/dest',
                '-p', str(patch_file), '/app']
        _run_main(argv, client)
        df = client.get_dockerfile()
        copy_pos = df.index('COPY ["copy-00000000')
        patch_pos = df.index('# patch 0000')
        assert copy_pos < patch_pos


# ---------------------------------------------------------------------------
# 7. Tag handling
# ---------------------------------------------------------------------------

class TestTagHandling:
    """Test tag generation and --tag-time behaviour."""

    def test_default_time_tag(self):
        """Without explicit tags, a time-based tag is used."""
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-c', 'true']
        _run_main(argv, client)
        tag = client.build_calls[0]['tag']
        # Format: img:YYYYMMDDHHMMSS
        assert tag.startswith('img:')
        tag_part = tag.split(':')[1]
        assert tag_part.isdigit()
        assert len(tag_part) == 14

    def test_explicit_tags(self):
        """Explicit tags are used."""
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-t', 'v1.0', '-c', 'true']
        _run_main(argv, client)
        tag = client.build_calls[0]['tag']
        assert tag.startswith('img:')

    def test_tag_time_with_explicit_tags(self):
        """--tag-time adds time tag even when explicit tags exist."""
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-t', 'v1.0', '--tag-time', '-c', 'true']
        _run_main(argv, client)
        tag = client.build_calls[0]['tag']
        assert 'img:' in tag


# ---------------------------------------------------------------------------
# 8. Docker build - success and failure paths
# ---------------------------------------------------------------------------

class TestDockerBuild:
    """Test build success cleanup and build failure preservation."""

    def test_success_cleans_up(self):
        """On successful build, the temp directory is cleaned up."""
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-c', 'true']
        _run_main(argv, client)
        build_path = client.build_calls[0]['path']
        # After successful build, the temp dir contents should be cleaned
        assert not (pathlib.Path(build_path) / 'Dockerfile').exists()

    def test_failure_preserves_temp_dir(self, capsys):
        """On build failure, the temp directory is preserved for inspection."""
        import docker as docker_mod
        build_error = docker_mod.errors.BuildError(
            'build failed', [{'stream': 'error log\n'}])
        client = _make_client(build_error=build_error)
        argv = ['prog', '-b', 'img:tag', '-c', 'true']
        with pytest.raises(SystemExit) as exc_info:
            _run_main(argv, client)
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert 'Leaving docker filesystem intact' in captured.err

    def test_failure_preserves_dockerfile_on_disk(self):
        """On build failure, the Dockerfile remains on disk for inspection."""
        import docker as docker_mod
        build_error = docker_mod.errors.BuildError(
            'build failed', [{'stream': 'error log\n'}])
        client = _make_client(build_error=build_error)
        argv = ['prog', '-b', 'img:tag', '-c', 'true']
        with pytest.raises(SystemExit):
            _run_main(argv, client)
        build_path = client.build_calls[0]['path']
        # Temp dir should still exist with Dockerfile
        assert (pathlib.Path(build_path) / 'Dockerfile').exists()
        # Clean up manually
        shutil.rmtree(build_path, ignore_errors=True)

    def test_build_passes_nocache(self):
        """--no-cache is forwarded to docker build."""
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '--no-cache', '-c', 'true']
        _run_main(argv, client)
        assert client.build_calls[0]['nocache'] is True

    def test_build_passes_network(self):
        """--network is forwarded to docker build."""
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '--network', 'host', '-c', 'true']
        _run_main(argv, client)
        assert client.build_calls[0]['network_mode'] == 'host'

    def test_build_path_is_valid_directory(self):
        """The path passed to docker build is a real directory with Dockerfile."""
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-c', 'true']
        _run_main(argv, client)
        # Snapshot was taken; verify Dockerfile was present at build time
        assert 'Dockerfile' in client.build_context_files
        df = client.get_dockerfile()
        assert 'FROM img:tag' in df


# ---------------------------------------------------------------------------
# 9. Docker pull failures
# ---------------------------------------------------------------------------

class TestDockerPull:
    """Test error handling around docker pull."""

    def test_docker_not_running(self):
        """docker.from_env() failure -> exit with error."""
        argv = ['prog', '-b', 'img:tag', '-c', 'true']
        with mock.patch('sys.argv', argv), \
             mock.patch('docker.from_env',
                        side_effect=Exception('connection refused')):
            with pytest.raises(SystemExit) as exc_info:
                docker_image_patch.main()
            assert exc_info.value.code == 1

    def test_image_not_found(self):
        """Pull failure (NotFound) -> exit with error."""
        import docker as docker_mod
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-c', 'true']
        with mock.patch('sys.argv', argv), \
             mock.patch('docker.from_env', return_value=client), \
             mock.patch.object(client, 'pull',
                               side_effect=docker_mod.errors.NotFound('nope')):
            with pytest.raises(SystemExit) as exc_info:
                docker_image_patch.main()
            assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# 10. Push logic
# ---------------------------------------------------------------------------

class TestPushImage:
    """Test --push-image functionality."""

    def test_push_success(self, capsys):
        """Successful push prints pushed message."""
        push_data = [
            b'{"status": "Pushing"}\n',
            b'{"status": "Pushed"}\n',
        ]
        client = _make_client(push_results=push_data)
        argv = ['prog', '-b', 'img:tag', '-c', 'true', '--push-image']
        _run_main(argv, client)
        assert len(client.push_calls) >= 1
        captured = capsys.readouterr()
        assert 'Pushed' in captured.out

    def test_push_error(self, capsys):
        """Push error -> exit with error."""
        push_data = [
            b'{"error": "denied: access forbidden"}\n',
        ]
        client = _make_client(push_results=push_data)
        argv = ['prog', '-b', 'img:tag', '-c', 'true', '--push-image']
        with pytest.raises(SystemExit) as exc_info:
            _run_main(argv, client)
        assert exc_info.value.code == 1

    def test_no_push_by_default(self, capsys):
        """Without --push-image, push is not called."""
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-c', 'true']
        _run_main(argv, client)
        assert len(client.push_calls) == 0
        captured = capsys.readouterr()
        assert 'docker push' in captured.out


# ---------------------------------------------------------------------------
# 11. Build context integrity
# ---------------------------------------------------------------------------

class TestBuildContext:
    """Test that the build context directory is set up correctly."""

    def test_dockerfile_ends_with_newline(self):
        """Generated Dockerfile ends with a newline."""
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-c', 'true']
        _run_main(argv, client)
        df = client.get_dockerfile()
        assert df.endswith('\n')

    def test_patch_file_content_preserved(self, tmp_path):
        """Patch file content is written exactly as provided."""
        patch_content = textwrap.dedent("""\
            diff --git a/foo.py b/foo.py
            index 1234567..abcdefg 100644
            --- a/foo.py
            +++ b/foo.py
            @@ -1,3 +1,4 @@
             line1
            +added line
             line2
             line3
        """)
        patch_file = tmp_path / 'complex.patch'
        patch_file.write_text(patch_content)

        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-p', str(patch_file), '/app']
        _run_main(argv, client)
        patches = client.list_files('*.patch')
        assert len(patches) == 1
        assert client.get_file(patches[0]) == patch_content

    def test_build_context_has_only_expected_files(self):
        """Build context contains only Dockerfile (when only --run-before)."""
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-c', 'true']
        _run_main(argv, client)
        assert sorted(client.build_context_files.keys()) == ['Dockerfile']

    def test_build_context_with_patches_has_expected_files(self, tmp_path):
        """Build context contains Dockerfile + patch files."""
        p = tmp_path / 'my.patch'
        p.write_text('diff\n')
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-p', str(p), '/app']
        _run_main(argv, client)
        files = sorted(client.build_context_files.keys())
        assert 'Dockerfile' in files
        patch_files = [f for f in files if f.endswith('.patch')]
        assert len(patch_files) == 1


# ---------------------------------------------------------------------------
# 12. Full integration-style: combined features
# ---------------------------------------------------------------------------

class TestCombined:
    """Integration-style tests combining multiple features."""

    def test_full_workflow(self, tmp_path):
        """Full workflow: copy + patch + run-before + run-after."""
        # Set up source file and patch
        src_file = tmp_path / 'config.yaml'
        src_file.write_text('key: value')
        patch_file = tmp_path / 'fix.patch'
        patch_file.write_text('diff content\n')

        client = _make_client(
            pull_attrs={'Config': {'User': 'app', 'WorkDir': '/opt/app'}})
        argv = [
            'prog', '-b', 'registry.io/org/myapp:v1.2.3',
            '-w', '/opt/app',
            '--docker-user', 'app',
            '--copy', str(src_file), '/etc/app/config.yaml',
            '-c', 'apt-get update', 'pip install deps',
            '-p', str(patch_file), '/opt/app',
            '--run-after', 'rm -rf /tmp/*',
            '-t', 'patched',
        ]
        _run_main(argv, client)

        df = client.get_dockerfile()

        # Verify ordering of Dockerfile sections
        from_pos = df.index('FROM registry.io/org/myapp:v1.2.3')
        user_root_pos = df.index('USER root')
        copy_pos = df.index('COPY ["copy-00000000')
        run_before_pos = df.index('RUN apt-get update')
        patch_pos = df.index('# patch 0000')
        run_after_pos = df.index('RUN rm -rf /tmp/*')
        workdir_pos = df.rindex('WORKDIR "/opt/app"')
        user_app_pos = df.rindex('USER "app"')

        assert from_pos < user_root_pos
        assert user_root_pos < copy_pos
        assert copy_pos < run_before_pos
        assert run_before_pos < patch_pos
        assert patch_pos < run_after_pos
        assert run_after_pos < workdir_pos
        assert workdir_pos < user_app_pos

        # Verify build context files
        assert client.get_file('copy-00000000/config.yaml') == 'key: value'
        assert len(client.list_files('*.patch')) == 1

    def test_quiet_suppresses_pull_message(self, capsys):
        """--quiet suppresses the pull message."""
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-q', '-c', 'true']
        _run_main(argv, client)
        captured = capsys.readouterr()
        assert 'Pulling' not in captured.out

    def test_quiet_suppresses_build_log(self, capsys):
        """--quiet suppresses the build log on error."""
        import docker as docker_mod
        build_error = docker_mod.errors.BuildError(
            'build failed', [{'stream': 'secret log\n'}])
        client = _make_client(build_error=build_error)
        argv = ['prog', '-b', 'img:tag', '-q', '-c', 'true']
        with pytest.raises(SystemExit):
            _run_main(argv, client)
        captured = capsys.readouterr()
        # quiet mode should suppress the build log
        assert 'secret log' not in captured.out


# ---------------------------------------------------------------------------
# 13. Edge cases: Repository derivation
# ---------------------------------------------------------------------------

class TestRepositoryEdgeCases:
    """Edge cases for --repository defaulting from base image name."""

    def test_repo_from_image_with_port(self):
        """registry:5000/image:tag -> repo = 'registry:5000/image'."""
        client = _make_client()
        argv = ['prog', '-b', 'registry:5000/image:tag', '--run-before', 'echo hi']
        _run_main(argv, client)
        # split(":") gives ['registry', '5000/image', 'tag'], [:-1] joined = 'registry5000/image'
        # This is the existing behaviour (lossy join for port-based registries).
        tag = client.build_calls[0]['tag']
        assert tag.startswith('registry5000/image:')

    def test_repo_from_image_with_multiple_colons(self):
        """Image string with multiple colons — repo derived by dropping last segment."""
        client = _make_client()
        argv = ['prog', '-b', 'host:port/path:v1', '--run-before', 'true']
        _run_main(argv, client)
        tag = client.build_calls[0]['tag']
        # "host:port/path:v1".split(":") = ['host', 'port/path', 'v1']
        # [:-1] = ['host', 'port/path'], "".join(...) = 'hostport/path'
        assert tag.startswith('hostport/path:')


# ---------------------------------------------------------------------------
# 14. Edge cases: Patch naming
# ---------------------------------------------------------------------------

class TestPatchNamingEdgeCases:
    """Edge cases for patch file naming logic."""

    def test_patch_file_already_ending_in_dot_patch(self, tmp_path):
        """Patch file named 'fix.patch' should NOT get double .patch extension."""
        p = tmp_path / 'fix.patch'
        p.write_text('diff\n')
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-p', str(p), '/app']
        _run_main(argv, client)
        patches = client.list_files('*.patch')
        assert len(patches) == 1
        assert patches[0] == '0000-fix.patch'
        assert not patches[0].endswith('.patch.patch')

    def test_patch_file_not_ending_in_dot_patch(self, tmp_path):
        """Patch file named 'fix.diff' should get .patch appended."""
        p = tmp_path / 'fix.diff'
        p.write_text('diff\n')
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-p', str(p), '/app']
        _run_main(argv, client)
        patches = client.list_files('*.patch')
        assert len(patches) == 1
        assert patches[0] == '0000-fix.diff.patch'

    def test_git_ref_with_slash_and_dotdot(self):
        """Ref like 'origin/main..origin/fix' gets slashes replaced, no -HEAD+staged."""
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-g', 'origin/main..origin/fix', '/app']
        _run_main(argv, client, [
            mock.patch('subprocess.check_output',
                       return_value=b'diff content\n'),
        ])
        patches = client.list_files('*.patch')
        assert len(patches) == 1
        # slashes replaced, '..' present so no '-HEAD+staged'
        assert patches[0] == '0000-origin_main..origin_fix.patch'

    def test_multiple_git_patches_numbered_sequentially(self):
        """Multiple --git flags produce sequentially numbered patch files."""
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-g', '/app1', '-g', '/app2']
        _run_main(argv, client, [
            mock.patch('subprocess.check_output',
                       return_value=b'diff content\n'),
        ])
        patches = client.list_files('*.patch')
        assert len(patches) == 2
        assert patches[0].startswith('0000-')
        assert patches[1].startswith('0001-')

    def test_mixed_git_and_patch_numbered_sequentially(self, tmp_path):
        """Mixed --git and --patch produce a single sequential counter."""
        p = tmp_path / 'a.patch'
        p.write_text('diff\n')
        client = _make_client()
        # -g first, then -p
        argv = ['prog', '-b', 'img:tag', '-g', '/app', '-p', str(p), '/app2']
        _run_main(argv, client, [
            mock.patch('subprocess.check_output',
                       return_value=b'git diff\n'),
        ])
        patches = client.list_files('*.patch')
        assert len(patches) == 2
        assert patches[0].startswith('0000-')
        assert patches[1].startswith('0001-')


# ---------------------------------------------------------------------------
# 15. Edge cases: Git diff subprocess call arguments
# ---------------------------------------------------------------------------

class TestGitDiffArgs:
    """Verify subprocess.check_output is called with expected git arguments."""

    def test_git_1arg_uses_cwd_and_head(self):
        """--git /app -> git diff HEAD in cwd."""
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-g', '/app']
        with mock.patch('sys.argv', argv), \
             mock.patch('docker.from_env', return_value=client), \
             mock.patch.object(client, 'pull', return_value=client._pull_image), \
             mock.patch('subprocess.check_output',
                        return_value=b'diff\n') as mock_git:
            docker_image_patch.main()
        call_args = mock_git.call_args[0][0]
        assert call_args[0] == 'git'
        assert call_args[1:3] == ['-C', '.']
        assert call_args[3] == 'diff'
        assert call_args[4] == 'HEAD'

    def test_git_2arg_uses_custom_ref(self):
        """--git abc123 /app -> git diff abc123 in cwd."""
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-g', 'abc123', '/app']
        with mock.patch('sys.argv', argv), \
             mock.patch('docker.from_env', return_value=client), \
             mock.patch.object(client, 'pull', return_value=client._pull_image), \
             mock.patch('subprocess.check_output',
                        return_value=b'diff\n') as mock_git:
            docker_image_patch.main()
        call_args = mock_git.call_args[0][0]
        assert call_args[1:3] == ['-C', '.']
        assert call_args[4] == 'abc123'

    def test_git_3arg_uses_custom_path_and_ref(self):
        """--git /repo main /app -> git -C /repo diff main."""
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-g', '/repo', 'main', '/app']
        with mock.patch('sys.argv', argv), \
             mock.patch('docker.from_env', return_value=client), \
             mock.patch.object(client, 'pull', return_value=client._pull_image), \
             mock.patch('subprocess.check_output',
                        return_value=b'diff\n') as mock_git:
            docker_image_patch.main()
        call_args = mock_git.call_args[0][0]
        assert call_args[1:3] == ['-C', '/repo']
        assert call_args[4] == 'main'


# ---------------------------------------------------------------------------
# 16. Edge cases: Base image Config
# ---------------------------------------------------------------------------

class TestBaseImageConfig:
    """Edge cases for base image config extraction."""

    def test_config_missing_user_key(self):
        """Config with no 'User' key -> defaults to '' (no USER line)."""
        client = _make_client(
            pull_attrs={'Config': {'WorkDir': '/app'}})
        argv = ['prog', '-b', 'img:tag', '-c', 'true']
        _run_main(argv, client)
        df = client.get_dockerfile()
        lines = df.strip().splitlines()
        user_lines = [l for l in lines if l.startswith('USER')]
        assert user_lines == ['USER root']

    def test_config_missing_workdir_key(self):
        """Config with no 'WorkDir' key -> defaults to '/'."""
        client = _make_client(
            pull_attrs={'Config': {'User': 'app'}})
        argv = ['prog', '-b', 'img:tag', '-c', 'true']
        _run_main(argv, client)
        df = client.get_dockerfile()
        assert 'WORKDIR "/"' in df

    def test_config_completely_empty(self):
        """Config with no User or WorkDir -> defaults applied."""
        client = _make_client(pull_attrs={'Config': {}})
        argv = ['prog', '-b', 'img:tag', '-c', 'true']
        _run_main(argv, client)
        df = client.get_dockerfile()
        assert 'WORKDIR "/"' in df
        lines = df.strip().splitlines()
        user_lines = [l for l in lines if l.startswith('USER')]
        assert user_lines == ['USER root']


# ---------------------------------------------------------------------------
# 17. Edge cases: Tag handling
# ---------------------------------------------------------------------------

class TestTagEdgeCases:
    """Additional tag handling edge cases."""

    def test_explicit_tag_no_time_tag_when_tag_time_not_set(self):
        """With explicit tags and no --tag-time, time tag is still added
        (because the condition is `not args.tags` OR tag_time)."""
        client = _make_client()
        # Note: the code adds time tag if `not args.tags` OR `args.tag_time`.
        # With explicit tags AND no --tag-time, the first condition is False,
        # the second is None (falsy), so time tag is NOT added... except
        # actually `not args.tags` is False, and `args.tag_time and ...` is
        # None (falsy), so the whole condition is False. Time tag is NOT added.
        argv = ['prog', '-b', 'img:tag', '-t', 'v1.0', '-c', 'true']
        _run_main(argv, client)
        tag = client.build_calls[0]['tag']
        assert tag == 'img:v1.0'

    def test_additional_tags_applied_via_image_tag(self):
        """Multiple --tags results in image.tag() called for each extra tag."""
        build_image = FakeImage()
        client = _make_client(build_image=build_image)
        argv = ['prog', '-b', 'img:tag', '-t', 'first', 'second', 'third',
                '-c', 'true']
        _run_main(argv, client)
        # First tag is used in the build call, the rest via image.tag()
        assert client.build_calls[0]['tag'] == 'img:first'
        assert 'second' in build_image._tags
        assert 'third' in build_image._tags

    def test_no_push_output_lists_all_tags(self, capsys):
        """Without --push-image, all fq tags are printed as push suggestions."""
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-t', 'a', 'b', '-c', 'true']
        _run_main(argv, client)
        captured = capsys.readouterr()
        assert 'docker push img:a' in captured.out
        assert 'docker push img:b' in captured.out


# ---------------------------------------------------------------------------
# 18. Edge cases: Build error messages and cleanup
# ---------------------------------------------------------------------------

class TestBuildErrorEdgeCases:
    """Additional tests for build error handling."""

    def test_build_error_message_contains_temp_path(self, capsys):
        """Build failure message includes the temp directory path."""
        import docker as docker_mod
        build_error = docker_mod.errors.BuildError(
            'step 3 failed', [{'stream': 'log\n'}])
        client = _make_client(build_error=build_error)
        argv = ['prog', '-b', 'img:tag', '-c', 'true']
        with pytest.raises(SystemExit):
            _run_main(argv, client)
        captured = capsys.readouterr()
        # The stderr message should contain a real temp path
        assert 'docker-live-patch' in captured.err
        assert 'Leaving docker filesystem intact' in captured.err

    def test_build_error_message_contains_error_text(self, capsys):
        """Build failure stderr includes the BuildError message."""
        import docker as docker_mod
        build_error = docker_mod.errors.BuildError(
            'step 5 kaboom', [{'stream': 'log\n'}])
        client = _make_client(build_error=build_error)
        argv = ['prog', '-b', 'img:tag', '-c', 'true']
        with pytest.raises(SystemExit):
            _run_main(argv, client)
        captured = capsys.readouterr()
        assert 'step 5 kaboom' in captured.err

    def test_non_quiet_build_error_prints_log(self, capsys):
        """Non-quiet build failure prints the build log to stdout."""
        import docker as docker_mod
        build_error = docker_mod.errors.BuildError(
            'failed', [{'stream': 'Step 1/3 : FROM img\n'},
                        {'stream': 'Step 2/3 : RUN bad\n'}])
        client = _make_client(build_error=build_error)
        argv = ['prog', '-b', 'img:tag', '-c', 'true']
        with pytest.raises(SystemExit):
            _run_main(argv, client)
        captured = capsys.readouterr()
        assert 'Step 1/3' in captured.out
        assert 'Step 2/3' in captured.out

    def test_success_build_log_printed_non_quiet(self, capsys):
        """On successful build, the build log is printed when not quiet."""
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-c', 'true']
        _run_main(argv, client)
        captured = capsys.readouterr()
        # FakeDockerClient returns [{'stream': 'ok\n'}] as the log
        assert 'ok' in captured.out

    def test_success_build_log_suppressed_quiet(self, capsys):
        """On successful build, the build log is NOT printed when quiet."""
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-q', '-c', 'true']
        _run_main(argv, client)
        captured = capsys.readouterr()
        assert 'Docker build log' not in captured.out


# ---------------------------------------------------------------------------
# 19. Edge cases: Push handling
# ---------------------------------------------------------------------------

class TestPushEdgeCases:
    """Additional push edge cases."""

    def test_push_multiple_tags(self, capsys):
        """--push-image with multiple tags pushes each one."""
        push_data = [b'{"status": "Pushed"}\n']
        client = _make_client(push_results=push_data)
        argv = ['prog', '-b', 'img:tag', '-t', 'a', 'b',
                '--push-image', '-c', 'true']
        _run_main(argv, client)
        assert len(client.push_calls) == 2
        assert 'img:a' in client.push_calls
        assert 'img:b' in client.push_calls

    def test_push_stream_multiple_json_per_chunk(self, capsys):
        """Push stream with multiple JSON objects in a single bytes chunk."""
        push_data = [
            b'{"status": "Preparing"}\n{"status": "Pushing"}\n{"status": "Pushed"}\n',
        ]
        client = _make_client(push_results=push_data)
        argv = ['prog', '-b', 'img:tag', '-c', 'true', '--push-image']
        _run_main(argv, client)
        captured = capsys.readouterr()
        assert 'Pushed' in captured.out

    def test_push_stream_error_with_status(self, capsys):
        """Push stream with both 'error' and 'status' in same JSON object."""
        push_data = [
            b'{"error": "denied", "status": "error occurred"}\n',
        ]
        client = _make_client(push_results=push_data)
        argv = ['prog', '-b', 'img:tag', '-c', 'true', '--push-image']
        with pytest.raises(SystemExit) as exc_info:
            _run_main(argv, client)
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert 'denied' in captured.out


# ---------------------------------------------------------------------------
# 20. Edge cases: Validation - valid standalone flags
# ---------------------------------------------------------------------------

class TestStandaloneFlags:
    """Verify that --run-after alone and --copy alone are valid invocations."""

    def test_run_after_alone_is_valid(self):
        """--run-after without --git or --patch is a valid invocation."""
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '--run-after', 'echo done']
        _run_main(argv, client)
        df = client.get_dockerfile()
        assert 'RUN echo done' in df

    def test_copy_alone_is_valid(self, tmp_path):
        """--copy without any patches is a valid invocation."""
        f = tmp_path / 'file.txt'
        f.write_text('data')
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '--copy', str(f), '/dest']
        _run_main(argv, client)
        df = client.get_dockerfile()
        assert 'COPY' in df

    def test_run_before_alone_is_valid(self):
        """--run-before alone is a valid invocation (already implicitly tested,
        but explicit is better)."""
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-c', 'apt-get update']
        _run_main(argv, client)
        df = client.get_dockerfile()
        assert 'RUN apt-get update' in df


# ---------------------------------------------------------------------------
# 21. Edge cases: Docker daemon error message
# ---------------------------------------------------------------------------

class TestDockerDaemonError:
    """Test error message content when docker daemon is unreachable."""

    def test_docker_daemon_error_message(self, capsys):
        """Error message includes the exception text."""
        argv = ['prog', '-b', 'img:tag', '-c', 'true']
        with mock.patch('sys.argv', argv), \
             mock.patch('docker.from_env',
                        side_effect=Exception('connection refused')):
            with pytest.raises(SystemExit):
                docker_image_patch.main()
        captured = capsys.readouterr()
        assert 'connection refused' in captured.out
        assert 'is it running' in captured.out

    def test_pull_error_message(self, capsys):
        """Pull NotFound error message includes the exception."""
        import docker as docker_mod
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-c', 'true']
        with mock.patch('sys.argv', argv), \
             mock.patch('docker.from_env', return_value=client), \
             mock.patch.object(client, 'pull',
                               side_effect=docker_mod.errors.NotFound(
                                   'image not found: img:tag')):
            with pytest.raises(SystemExit):
                docker_image_patch.main()
        captured = capsys.readouterr()
        assert 'Could not pull base image' in captured.err


# ---------------------------------------------------------------------------
# 22. Edge cases: git diff error messages
# ---------------------------------------------------------------------------

class TestGitDiffErrors:
    """Test error messages for git diff failures."""

    def test_git_diff_error_message(self, capsys):
        """CalledProcessError prints meaningful error message."""
        import subprocess as sp
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-g', '/app']
        with pytest.raises(SystemExit):
            _run_main(argv, client, [
                mock.patch('subprocess.check_output',
                           side_effect=sp.CalledProcessError(128, 'git')),
            ])
        captured = capsys.readouterr()
        assert 'Could not acquire git diff' in captured.err
        assert 'is the git path correct' in captured.err

    def test_empty_diff_error_message(self, capsys):
        """Empty diff prints error with git path and ref."""
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-g', 'myref', '/app']
        with pytest.raises(SystemExit):
            _run_main(argv, client, [
                mock.patch('subprocess.check_output', return_value=b'\n'),
            ])
        captured = capsys.readouterr()
        assert 'empty' in captured.out.lower()
        assert 'myref' in captured.out


# ---------------------------------------------------------------------------
# 23. Edge cases: Temp directory lifecycle
# ---------------------------------------------------------------------------

class TestTempDirLifecycle:
    """Ensure temp directory has correct prefix and lifecycle semantics."""

    def test_temp_dir_has_docker_live_patch_prefix(self):
        """Build context path contains the 'docker-live-patch' prefix."""
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-c', 'true']
        _run_main(argv, client)
        build_path = client.build_calls[0]['path']
        assert 'docker-live-patch' in build_path

    def test_temp_dir_removed_after_success(self):
        """After successful build, temp directory is fully removed."""
        client = _make_client()
        argv = ['prog', '-b', 'img:tag', '-c', 'true']
        _run_main(argv, client)
        build_path = client.build_calls[0]['path']
        assert not pathlib.Path(build_path).exists()

    def test_temp_dir_exists_after_failure(self):
        """After build failure, temp directory still exists with all files."""
        import docker as docker_mod
        build_error = docker_mod.errors.BuildError(
            'fail', [{'stream': 'log\n'}])
        client = _make_client(build_error=build_error)
        argv = ['prog', '-b', 'img:tag', '-c', 'true']
        with pytest.raises(SystemExit):
            _run_main(argv, client)
        build_path = client.build_calls[0]['path']
        p = pathlib.Path(build_path)
        assert p.exists()
        assert (p / 'Dockerfile').exists()
        # cleanup
        shutil.rmtree(build_path, ignore_errors=True)

    def test_temp_dir_failure_preserves_patch_files(self, tmp_path):
        """After build failure, patch files remain in the temp directory."""
        import docker as docker_mod
        patch_file = tmp_path / 'fix.patch'
        patch_file.write_text('diff content\n')
        build_error = docker_mod.errors.BuildError(
            'fail', [{'stream': 'log\n'}])
        client = _make_client(build_error=build_error)
        argv = ['prog', '-b', 'img:tag', '-p', str(patch_file), '/app']
        with pytest.raises(SystemExit):
            _run_main(argv, client)
        build_path = client.build_calls[0]['path']
        p = pathlib.Path(build_path)
        patch_files = list(p.glob('*.patch'))
        assert len(patch_files) == 1
        assert patch_files[0].read_text() == 'diff content\n'
        # cleanup
        shutil.rmtree(build_path, ignore_errors=True)
