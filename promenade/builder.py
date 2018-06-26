from . import logging, renderer
from beaker.cache import CacheManager
from beaker.util import parse_cache_config_options

import io
import itertools
import os
import requests
import stat
import tarfile
import time

__all__ = ['Builder']

LOG = logging.getLogger(__name__)

# Ignore bandit false positive:
#   B108:hardcoded_tmp_directory
# This cache needs to be shared by all forks within the same container, and so
# must be at a well-known location.
CACHE_OPTS = {
    'cache.type': 'file',
    'cache.data_dir': '/tmp/cache/data',  # nosec
    'cache.lock_dir': '/tmp/cache/lock',  # nosec
}

CACHE = CacheManager(**parse_cache_config_options(CACHE_OPTS))


class Builder:
    def __init__(self, config, *, validators=False):
        self.config = config
        self.validators = validators
        self._file_cache = None

    @property
    def file_cache(self):
        if not self._file_cache:
            self._build_file_cache()
        return self._file_cache

    def _build_file_cache(self):
        self._file_cache = {}
        for file_spec in self._file_specs:
            path = file_spec['path']
            if 'content' in file_spec:
                data = file_spec['content']
            elif 'tar_url' in file_spec:
                data = _fetch_tar_content(file_spec['tar_url'],
                                          file_spec['tar_path'])
            self._file_cache[path] = {
                'path': path,
                'data': data,
                'mode': file_spec['mode'],
            }

    @property
    def _file_specs(self):
        return itertools.chain(
            self.config.get_path('HostSystem:files', []),
            self.config.get_path('Genesis:files', []))

    def build_all(self, *, output_dir):
        self.build_genesis(output_dir=output_dir)
        for node_document in self.config.iterate(
                schema='promenade/KubernetesNode/v1'):
            self.build_node(node_document, output_dir=output_dir)

        if self.validators:
            validate_script = renderer.render_template(
                self.config, template='scripts/validate-cluster.sh')
            _write_script(output_dir, 'validate-cluster.sh', validate_script)

    def build_genesis(self, *, output_dir):
        LOG.info('Building genesis script')
        sub_config = self.config.extract_genesis_config()
        tarball = renderer.build_tarball_from_roles(
            config=sub_config,
            roles=['common', 'genesis'],
            file_specs=self.file_cache.values())

        script = renderer.render_template(
            sub_config,
            template='scripts/genesis.sh',
            context={
                'tarball': tarball
            })

        _write_script(output_dir, 'genesis.sh', script)

        if self.validators:
            validate_script = renderer.render_template(
                sub_config, template='scripts/validate-genesis.sh')
            _write_script(output_dir, 'validate-genesis.sh', validate_script)

    def build_node(self, node_document, *, output_dir):
        node_name = node_document['metadata']['name']
        LOG.info('Building script for node %s', node_name)
        script = self.build_node_script(node_name)

        _write_script(output_dir, _join_name(node_name), script)

        if self.validators:
            validate_script = self._build_node_validate_script(node_name)
            _write_script(output_dir, 'validate-%s.sh' % node_name,
                          validate_script)

    def build_node_script(self, node_name):
        sub_config = self.config.extract_node_config(node_name)
        file_spec_paths = [
            f['path'] for f in self.config.get_path('HostSystem:files', [])
        ]
        file_specs = [self.file_cache[p] for p in file_spec_paths]
        tarball = renderer.build_tarball_from_roles(
            config=sub_config, roles=['common', 'join'], file_specs=file_specs)

        return renderer.render_template(
            sub_config,
            template='scripts/join.sh',
            context={
                'tarball': tarball
            })

    def _build_node_validate_script(self, node_name):
        sub_config = self.config.extract_node_config(node_name)
        return renderer.render_template(
            sub_config, template='scripts/validate-join.sh')


@CACHE.cache('fetch_tarball_content', expire=72 * 3600)
def _fetch_tar_content(url, path):
    content = _fetch_tar_url(url)
    f = io.BytesIO(content)
    tf = tarfile.open(fileobj=f, mode='r')
    buf_reader = tf.extractfile(path)
    return buf_reader.read()


@CACHE.cache('fetch_tarball_url', expire=72 * 3600)
def _fetch_tar_url(url):
    LOG.debug('Fetching url=%s', url)
    # NOTE(mark-burnett): Retry with linear backoff until we are killed, e.g.
    # by a timeout.
    for attempt in itertools.count():
        try:
            response = requests.get(url)
            response.raise_for_status()
            break
        except requests.exceptions.RequestException:
            backoff = 5 * attempt
            LOG.exception('Failed to fetch %s, retrying in %d seconds', url,
                          backoff)
            time.sleep(backoff)

    LOG.debug('Finished downloading url=%s', url)
    return response.content


def _join_name(node_name):
    return 'join-%s.sh' % node_name


def _write_script(output_dir, name, script):
    path = os.path.join(output_dir, name)
    with open(path, 'w') as f:
        f.write(script)

    os.chmod(
        path,
        os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
