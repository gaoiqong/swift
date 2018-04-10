# Copyright (c) 2015 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import errno
import json
import os
import time

from random import random

from collections import defaultdict
from eventlet import Timeout

from swift.common.direct_client import (direct_put_container,
                                        DirectClientException)
from swift.container.replicator import ContainerReplicator
from swift.container.backend import ContainerBroker, \
    RECORD_TYPE_SHARD_NODE, UNSHARDED, SHARDING, SHARDED, COLLAPSED
from swift.common import internal_client, db_replicator
from swift.common.exceptions import DeviceUnavailable
from swift.common.constraints import check_drive, CONTAINER_LISTING_LIMIT
from swift.common.ring.utils import is_local_device
from swift.common.utils import get_logger, config_true_value, \
    dump_recon_cache, whataremyips, Timestamp, ShardRange, GreenAsyncPile, \
    config_float_value, config_positive_int_value, \
    quorum_size, parse_overrides, Everything


def sharding_enabled(broker):
    # NB all shards will by default have been created with
    # X-Container-Sysmeta-Sharding set and will therefore be candidates for
    # sharding, along with explicitly configured root containers.
    sharding = broker.metadata.get('X-Container-Sysmeta-Sharding')
    if sharding and config_true_value(sharding[0]):
        return True
    # if broker has been marked deleted it will have lost sysmeta, but we still
    # need to process the broker (for example, to shrink any shard ranges) so
    # fallback to checking if it has any shard ranges
    if broker.get_shard_ranges():
        return True
    return False


def make_shard_ranges(broker, shard_data, shards_account_prefix):
    timestamp = Timestamp.now()
    shard_ranges = []
    for data in shard_data:
        # Make a copy so we don't mutate the original
        kwargs = data.copy()
        path = ShardRange.make_path(
            shards_account_prefix + broker.root_account,
            broker.root_container, broker.container,
            timestamp, kwargs.pop('index'))

        shard_ranges.append(ShardRange(path, timestamp, **kwargs))
    return shard_ranges


def find_missing_ranges(shard_ranges):
    """
    Find any ranges in the entire object namespace that are not covered by any
    shard range in the given list.

    :param shard_ranges: A list of :class:`~swift.utils.ShardRange`
    :return: a list of missing ranges
    """
    gaps = []
    if not shard_ranges:
        return ((ShardRange.MIN, ShardRange.MAX),)
    if shard_ranges[0].lower > ShardRange.MIN:
        gaps.append((ShardRange.MIN, shard_ranges[0].lower))
    for first, second in zip(shard_ranges, shard_ranges[1:]):
        if first.upper < second.lower:
            gaps.append((first.upper, second.lower))
    if shard_ranges[-1].upper < ShardRange.MAX:
        gaps.append((shard_ranges[-1].upper, ShardRange.MAX))
    return gaps


def find_overlapping_ranges(shard_ranges):
    """
    Find all pairs of overlapping ranges in the given list.

    :param shard_ranges: A list of :class:`~swift.utils.ShardRange`
    :return: a set of tuples, each tuple containing ranges that overlap with
        each other.
    """
    result = set()
    for shard_range in shard_ranges:
        overlapping = [sr for sr in shard_ranges
                       if shard_range != sr and shard_range.overlaps(sr)]
        if overlapping:
            overlapping.append(shard_range)
            overlapping.sort()
            result.add(tuple(overlapping))

    return result


class ContainerSharder(ContainerReplicator):
    """Shards containers."""

    def __init__(self, conf, logger=None):
        logger = logger or get_logger(conf, log_route='container-sharder')
        super(ContainerSharder, self).__init__(conf, logger=logger)
        self.shards_account_prefix = (
            (conf.get('auto_create_account_prefix') or '.') + 'shards_')

        try:
            self.shard_shrink_point = config_float_value(
                conf.get('shard_shrink_point', 25), 0, 100) / 100.0
        except ValueError as err:
            raise ValueError(err.message + ": shard_shrink_point")
        try:
            self.shrink_merge_point = config_float_value(
                conf.get('shard_shrink_merge_point', 75), 0, 100) / 100.0
        except ValueError as err:
            raise ValueError(err.message + ": shard_shrink_merge_point")
        self.shard_container_size = config_positive_int_value(
            conf.get('shard_container_size', 10000000))
        self.shrink_size = self.shard_container_size * self.shard_shrink_point
        self.merge_size = self.shard_container_size * self.shrink_merge_point
        self.split_size = self.shard_container_size // 2
        self.scanner_batch_size = config_positive_int_value(
            conf.get('shard_scanner_batch_size', 10))
        self.shard_batch_size = config_positive_int_value(
            conf.get('shard_batch_size', 2))
        self.reported = None
        self.auto_shard = config_true_value(conf.get('auto_shard', False))
        self.recon_candidates_limit = int(
            conf.get('recon_candidates_limit', 5))

        # internal client
        self.conn_timeout = float(conf.get('conn_timeout', 5))
        request_tries = config_positive_int_value(
            conf.get('request_tries', 3))
        internal_client_conf_path = conf.get('internal_client_conf_path',
                                             '/etc/swift/internal-client.conf')
        try:
            self.swift = internal_client.InternalClient(
                internal_client_conf_path,
                'Swift Container Sharder',
                request_tries,
                allow_modify_pipeline=False)
        except IOError as err:
            # TODO: if sharder functions are moved into replicator then for
            # backwards compatibility we need this to simply log a warning that
            # sharding will be skipped due to missing internal client conf
            if err.errno != errno.ENOENT:
                raise
            raise SystemExit(
                'Unable to load internal client from config: %r (%s)' %
                (internal_client_conf_path, err))

    def _zero_stats(self):
        """Zero out the stats."""
        super(ContainerSharder, self)._zero_stats()
        # all sharding stats that are additional to the inherited replicator
        # stats are maintained under the 'sharding' key in self.stats
        self.stats['sharding'] = defaultdict(lambda: defaultdict(int))

    def _append_stat(self, category, key, value):
        if not self.stats['sharding'][category][key]:
            self.stats['sharding'][category][key] = list()
        self.stats['sharding'][category][key].append(value)

    def _min_stat(self, category, key, value):
        current = self.stats['sharding'][category][key]
        if not current:
            self.stats['sharding'][category][key] = value
        else:
            self.stats['sharding'][category][key] = min(current, value)

    def _max_stat(self, category, key, value):
        current = self.stats['sharding'][category][key]
        if not current:
            self.stats['sharding'][category][key] = value
        else:
            self.stats['sharding'][category][key] = max(current, value)

    def _increment_stat(self, category, key, step=1):
        self.stats['sharding'][category][key] += step

    def _transform_sharding_candidate_stats(self):
        category = self.stats['sharding']['sharding_candidates']
        candidates = category.pop('all', [])
        category['found'] = len(candidates)
        candidates.sort(key=lambda c: c['object_count'], reverse=True)
        if self.recon_candidates_limit >= 0:
            category['top'] = candidates[:self.recon_candidates_limit]
        else:
            category['top'] = candidates

    def _report_stats(self):
        default_stats = ('attempted', 'success', 'failure')
        category_keys = (
            ('visited', default_stats + ('skipped',)),
            ('scanned', default_stats + ('found', 'min_time', 'max_time')),
            ('created', default_stats),
            ('cleaved', default_stats + ('min_time', 'max_time',)),
            ('misplaced', default_stats + ('found', 'placed', 'unplaced')),
            ('audit_root', default_stats),
            ('audit_shard', default_stats),
        )

        now = time.time()
        last_report = time.ctime(self.reported)
        elapsed = now - self.stats['start']
        sharding_stats = self.stats['sharding']
        for category, keys in category_keys:
            stats = sharding_stats[category]
            msg = ' '.join(['%s:%s' % (k, str(stats[k])) for k in keys])
            self.logger.info('Since %s %s - %s', last_report, category, msg)

        self._transform_sharding_candidate_stats()

        dump_recon_cache(
            {'sharding_stats': self.stats,
             'sharding_time': elapsed,
             'sharding_last': now},
            self.rcache, self.logger)
        self.reported = now

    def _periodic_report_stats(self):
        if (time.time() - self.reported) >= 3600:  # once an hour
            self._report_stats()
            self._zero_stats()

    def _fetch_shard_ranges(self, broker, newest=False, params=None):
        path = self.swift.make_path(broker.root_account, broker.root_container)
        params = params or {}
        params.setdefault('format', 'json')
        headers = {'X-Backend-Record-Type': 'shard'}
        if newest:
            headers['X-Newest'] = 'true'
        try:
            resp = self.swift.make_request(
                'GET', path, headers, acceptable_statuses=(2,), params=params)
        except internal_client.UnexpectedResponse:
            self.logger.error("Failed to get shard ranges from %s",
                              broker.root_path)
            return None

        return [ShardRange.from_dict(shard_range)
                for shard_range in json.loads(resp.body)]

    def _put_container(self, node, part, account, container, headers, body):
        try:
            direct_put_container(node, part, account, container,
                                 conn_timeout=self.conn_timeout,
                                 response_timeout=self.node_timeout,
                                 headers=headers, contents=body)
        except DirectClientException as err:
            self.logger.warning(
                'Failed to put shard ranges to %s:%s/%s: %s',
                node['ip'], node['port'], node['device'], err.http_status)
        except (Exception, Timeout) as err:
            self.logger.exception(
                'Failed to put shard ranges to %s:%s/%s: %s',
                node['ip'], node['port'], node['device'], err)
        else:
            return True
        return False

    def _send_shard_ranges(self, account, container, shard_ranges,
                           headers=None):
        body = json.dumps([dict(sr) for sr in shard_ranges])
        part, nodes = self.ring.get_nodes(account, container)
        headers = headers or {}
        headers.update({'X-Backend-Record-Type': RECORD_TYPE_SHARD_NODE,
                        'User-Agent': 'container-sharder %s' % os.getpid(),
                        'X-Timestamp': Timestamp.now().normal,
                        'Content-Length': len(body),
                        'Content-Type': 'application/json'})

        pool = GreenAsyncPile(len(nodes))
        for node in nodes:
            pool.spawn(self._put_container, node, part, account,
                       container, headers, body)

        results = pool.waitall(None)
        return results.count(True) >= quorum_size(self.ring.replica_count)

    def _get_shard_broker(self, shard_range, root_path, policy_index,
                          force=False):
        """
        Get a container broker for given shard range.

        :param shard_range: a :class:`~swift.common.utils.ShardRange`
        :param root_path: the path of the shard's root container
        :param policy_index: the storage policy index
        :param force: if True set the broker's sharding metadata even if
            sharding metadata already exists
        :returns: a shard container broker
        """
        part = self.ring.get_part(shard_range.account, shard_range.container)
        node = self.find_local_handoff_for_part(part)
        if not node:
            # TODO: and when *do* we cleave? maybe we should just be picking
            # one of the local devs
            raise DeviceUnavailable(
                'No mounted devices found suitable to Handoff sharded '
                'container %s in partition %s' % (shard_range.name, part))

        shard_broker = self._initialize_broker(
            node['device'], part, shard_range.account, shard_range.container,
            storage_policy_index=policy_index, epoch=shard_range.epoch)

        # Get the valid info into the broker.container, etc
        shard_broker.get_info()

        if not shard_broker.get_sharding_info('Root') or force:
            shard_broker.merge_shard_ranges([shard_range])
            shard_broker.update_sharding_info({'Root': root_path})
            shard_broker.update_metadata({
                'X-Container-Sysmeta-Sharding':
                    ('True', Timestamp.now().internal)})

        return part, shard_broker, node['id']

    def yield_objects(self, broker, src_shard_range, policy_index):
        """
        Iterates through all objects in ``src_shard_range`` in name order
        yielding them in lists of up to CONTAINER_LISTING_LIMIT length.

        :param broker: A :class:`~swift.container.backend.ContainerBroker`.
        :param src_shard_range: A :class:`~swift.common.utils.ShardRange`
            describing the source range.
        :param policy_index: The storage policy index.
        :return: a generator of tuples of (list of objects, broker info dict)
        """
        # Since we're going to change lower attribute in the loop...
        src_shard_range = src_shard_range.copy()

        # TODO: list_objects_iter transforms the timestamp, losing info
        # that we want to copy - see _transform_record - we need to
        # override that behaviour

        while True:
            info = broker.get_info()
            info['max_row'] = broker.get_max_row()
            objects = broker.get_objects(
                CONTAINER_LISTING_LIMIT,
                marker=str(src_shard_range.lower),
                end_marker=src_shard_range.end_marker,
                storage_policy_index=policy_index,
                include_deleted=True)
            yield objects, info

            if len(objects) < CONTAINER_LISTING_LIMIT:
                break
            src_shard_range.lower = objects[-1]['name']

    def yield_objects_to_shard_range(self, broker, src_shard_range,
                                     policy_index, dest_shard_ranges):
        """
        Iterates through all objects in ``src_shard_range`` to place them in
        destination shard ranges provided by the ``next_shard_range`` function.
        Yields tuples of (object list, destination shard range in which those
        objects belong). Note that the same destination shard range may be
        referenced in more than one yielded tuple.

        :param broker: A :class:`~swift.container.backend.ContainerBroker`.
        :param src_shard_range: A :class:`~swift.common.utils.ShardRange`
            describing the source range.
        :param policy_index: The storage policy index.
        :param dest_shard_ranges: A function which should return a list of
            destination shard ranges in name order.
        :return: a generator of tuples of
            (object list, shard range, broker info dict)
        """
        dest_shard_range_iter = dest_shard_range = None
        for objs, info in self.yield_objects(broker, src_shard_range,
                                             policy_index):
            if not objs:
                return

            def next_or_none(it):
                try:
                    return next(it)
                except StopIteration:
                    return None

            if dest_shard_range_iter is None:
                dest_shard_range_iter = iter(dest_shard_ranges())
                dest_shard_range = next_or_none(dest_shard_range_iter)

            unplaced = False
            last_index = next_index = 0
            for obj in objs:
                if dest_shard_range is None:
                    # no more destinations: yield remainder of batch and return
                    # NB there may be more batches of objects but none of them
                    # will be placed so no point fetching them
                    yield objs[last_index:], None, info
                    return
                if obj['name'] <= dest_shard_range.lower:
                    unplaced = True
                elif unplaced:
                    # end of run of unplaced objects, yield them
                    yield objs[last_index:next_index], None, info
                    last_index = next_index
                    unplaced = False
                while (dest_shard_range and
                       obj['name'] > dest_shard_range.upper):
                    if next_index != last_index:
                        # yield the objects in current dest_shard_range
                        yield (objs[last_index:next_index],
                               dest_shard_range,
                               info)
                    last_index = next_index
                    dest_shard_range = next_or_none(dest_shard_range_iter)
                next_index += 1

            if next_index != last_index:
                # yield tail of current batch of objects
                # NB there may be more objects for the current
                # dest_shard_range in the next batch from yield_objects
                yield (objs[last_index:next_index],
                       None if unplaced else dest_shard_range,
                       info)

    def _replicate_and_delete(self, broker, policy_index, dest_shard_range,
                              part, dest_broker, node_id, info):
        success, responses = self._replicate_object(
            part, dest_broker.db_file, node_id)
        quorum = quorum_size(self.ring.replica_count)
        if not success and responses.count(True) < quorum:
            self.logger.warning(
                'Failed to sufficiently replicate misplaced objects: %s in %s '
                '(not removing)', dest_shard_range, broker.path)
            return False

        with broker.sharding_lock():
            # TODO: check we're actually contending for this lock when
            # modifying the broker dbs
            if broker.get_info()['id'] != info['id']:
                # the db changed - don't remove any objects
                success = False
            else:
                # remove objects up to the max row of the db sampled prior to
                # the first object yielded for this destination; objects added
                # after that point may not have been yielded and replicated so
                # it is not safe to remove them yet
                broker.remove_objects(
                    str(dest_shard_range.lower), str(dest_shard_range.upper),
                    policy_index, info['max_row'])
                success = True
        if not success:
            self.logger.warning(
                'Refused to remove misplaced objects: %s in %s',
                dest_shard_range, broker.path)
        return success

    def _move_objects(self, src_broker, src_shard_range, policy_index,
                      shard_range_fetcher):
        # move objects from src_shard_range in src_broker to destination shard
        # ranges provided by shard_range_fetcher
        dest_brokers = {}  # map shard range -> broker
        placed = unplaced = 0
        success = True
        for objs, dest_shard_range, info in self.yield_objects_to_shard_range(
                src_broker, src_shard_range, policy_index,
                shard_range_fetcher):
            if not dest_shard_range:
                unplaced += len(objs)
                success = False
                continue

            if dest_shard_range.name == src_broker.path:
                # in shrinking context, the misplaced objects might actually be
                # correctly placed if the root has expanded this shard but this
                # broker has not yet been updated
                continue

            if dest_shard_range not in dest_brokers:
                part, dest_broker, node_id = self._get_shard_broker(
                    dest_shard_range, src_broker.root_path, policy_index)
                # save the broker info that was sampled prior to the *first*
                # yielded objects for this destination
                destination = {'part': part,
                               'dest_broker': dest_broker,
                               'node_id': node_id,
                               'info': info}
                dest_brokers[dest_shard_range] = destination
            else:
                destination = dest_brokers[dest_shard_range]
            destination['dest_broker'].merge_items(objs)
            placed += len(objs)

        if unplaced:
            self.logger.warning(
                'Failed to find destination for at least %s misplaced objects '
                'in %s' % (unplaced, src_broker.path))

        # TODO: consider executing the replication jobs concurrently
        for dest_shard_range, dest_args in dest_brokers.items():
            self.logger.debug('moving misplaced objects found in range %s' %
                              dest_shard_range)
            success &= self._replicate_and_delete(
                src_broker, policy_index, dest_shard_range, **dest_args)

        self._increment_stat('misplaced', 'placed', step=placed)
        self._increment_stat('misplaced', 'unplaced', step=unplaced)
        self.logger.debug('Moved %s misplaced objects' % placed)
        return success, placed + unplaced

    def _make_shard_range_fetcher(self, broker):
        # returns a function that will lazy load shard ranges on demand;
        # this means only one lookup is made for all misplaced ranges.
        outer = {}

        def shard_range_fetcher():
            if not outer:
                if broker.is_root_container():
                    ranges = broker.get_shard_ranges()
                else:
                    # TODO: the root may not yet know about shard ranges to
                    # which a shard is sharding - those need to come from
                    # the broker
                    ranges = self._fetch_shard_ranges(broker, newest=True)
                outer['ranges'] = iter(ranges)
            return outer['ranges']
        return shard_range_fetcher

    def _make_default_misplaced_object_bounds(self, broker):
        # Objects outside of this container's own range are misplaced.
        own_shard_range = broker.get_own_shard_range()
        bounds = []
        if own_shard_range.lower:
            bounds.append(('', own_shard_range.lower))
        if own_shard_range.upper:
            bounds.append((own_shard_range.upper, ''))
        return bounds

    def _make_misplaced_object_bounds(self, broker):
        self.logger.debug('Looking for misplaced objects in %s (%s)',
                          broker.path, broker.db_file)
        self._increment_stat('misplaced', 'attempted')

        bounds = []
        state = broker.get_db_state()
        if state == SHARDED:
            # Anything in the object table is treated as a misplaced object.
            bounds.append(('', ''))

        if not bounds and state == SHARDING:
            # Objects outside of this container's own range are misplaced.
            # Objects in already cleaved shard ranges are also misplaced.
            # TODO: if we redirect new udpates to CREATED shards then should we
            # also treat objects in CREATED shards as misplaced, as well as
            # ACTIVE shards?
            cleave_context = broker.load_cleave_context()
            cleave_cursor = cleave_context.get('cursor')
            if cleave_cursor:
                bounds.append(('', cleave_cursor))
                own_shard_range = broker.get_own_shard_range()
                if own_shard_range.upper:
                    bounds.append((own_shard_range.upper, ''))

        return bounds or self._make_default_misplaced_object_bounds(broker)

    def _move_misplaced_objects(self, broker, src_broker=None,
                                src_bounds=None):
        """
        Search for objects in the given broker that do not belong in that
        broker's namespace and move those objects to their correct shard
        container.

        :param broker: An instance of :class:`swift.container.ContainerBroker`.
        :param src_broker: optional alternative broker to use as the source
            of misplaced objects; if not specified then ``broker`` is used as
            the source.
        :param src_bounds: optional list of (lower, upper) namespace bounds to
            use when searching for misplaced objects
        :return: True if all misplaced objects were sufficiently replicated to
            their correct shard containers, False otherwise
        """
        if broker.is_deleted():
            self.logger.debug('Not looking for misplaced objects in deleted '
                              'container %s (%s)', broker.path, broker.db_file)
            return True

        src_broker = src_broker or broker
        if src_bounds is None:
            src_bounds = self._make_misplaced_object_bounds(broker)
        src_ranges = [ShardRange('dont/care', Timestamp.now(), lower, upper)
                      for lower, upper in src_bounds]
        self.logger.debug('misplaced object source bounds %s' % src_bounds)
        policy_index = broker.storage_policy_index
        # TODO: what about records for objects in the wrong storage policy?
        success = True
        num_found = 0
        for query in src_ranges:
            part_success, part_num_found = self._move_objects(
                src_broker, query, policy_index,
                self._make_shard_range_fetcher(broker))
            success &= part_success
            num_found += part_num_found

        if num_found:
            self.logger.increment('misplaced_items_found')
            self._increment_stat('misplaced', 'found')
        self._increment_stat('misplaced', 'success' if success else 'failure')
        self.logger.info('Finished misplaced shard replication')
        return success

    def _post_replicate_hook(self, broker, info, responses):
        return

    def _audit_root_container(self, broker):
        # This is the root container, and therefore the tome of knowledge,
        # all we can do is check there is nothing screwy with the ranges
        self._increment_stat('audit_root', 'attempted')
        warnings = []
        own_shard_range = broker.get_own_shard_range()

        if own_shard_range.state in (ShardRange.SHARDING, ShardRange.SHARDED):
            shard_ranges = broker.get_shard_ranges()
            missing_ranges = find_missing_ranges(shard_ranges)
            if missing_ranges:
                warnings.append(
                    'missing range(s): %s' %
                    ' '.join(['%s-%s' % (lower, upper)
                              for lower, upper in missing_ranges]))

        for state in ShardRange.STATES:
            shard_ranges = broker.get_shard_ranges(states=state)
            overlaps = find_overlapping_ranges(shard_ranges)
            for overlapping_ranges in overlaps:
                warnings.append(
                    'overlapping ranges in state %s: %s' %
                    (ShardRange.STATES[state],
                     ' '.join(['%s-%s' % (sr.lower, sr.upper)
                               for sr in overlapping_ranges])))

        if warnings:
            self.logger.warning(
                'Audit failed for root %s (%s): %s' %
                (broker.db_file, broker.path, ', '.join(warnings)))
            self.logger.increment('audit_failed')
            self._increment_stat('audit_root', 'failure')
            return False

        self._increment_stat('audit_root', 'success')
        return True

    def _audit_shard_container(self, broker):
        # Get the root view of the world.
        self._increment_stat('audit_shard', 'attempted')
        warnings = []
        errors = []
        if not broker.account.startswith(self.shards_account_prefix):
            warnings.append('account not in shards namespace %r' %
                            self.shards_account_prefix)

        own_shard_range = broker.get_own_shard_range(no_default=True)
        if not own_shard_range:
            errors.append('missing own shard range')

        # try to contact root container
        upper = own_shard_range.upper if own_shard_range else ''
        shard_ranges = self._fetch_shard_ranges(
            broker, newest=True, params={'includes': upper})

        shard_range = None
        if shard_ranges and own_shard_range:
            for shard_range in shard_ranges:
                if (shard_range.lower == own_shard_range.lower and
                        shard_range.upper == own_shard_range.upper and
                        shard_range.name == own_shard_range.name):
                    break
            else:
                shard_range = None

        if not shard_range:
            # this is not necessarily an error - some replicas of the root may
            # not yet know about this shard container
            warnings.append('root has no matching shard range')

        if warnings:
            self.logger.warning(
                'Audit warnings for shard %s (%s): %s' %
                (broker.db_file, broker.path, ', '.join(warnings)))

        if errors:
            self.logger.warning(
                'Audit failed for shard %s (%s) - skipping: %s' %
                (broker.db_file, broker.path, ', '.join(errors)))
            self.logger.increment('audit_failed')
            self._increment_stat('audit_shard', 'failure')
            return False

        if shard_range:
            # TODO: merging the root shard range here may save us having to
            # push every shard range from the root in process_broker
            broker.merge_shard_ranges([shard_range])
            own_shard_range = broker.get_own_shard_range()
            delete_age = time.time() - self.reclaim_age
            if (own_shard_range.state == ShardRange.SHARDED and
                    own_shard_range.deleted and
                    own_shard_range.timestamp < delete_age and
                    broker.empty()):
                broker.delete_db(Timestamp.now().internal)
                self.logger.debug('Deleted shard container %s (%s)',
                                  broker.db_file, broker.path)
        self._increment_stat('audit_shard', 'success')
        return True

    def _audit_container(self, broker):
        if broker.is_deleted():
            # if the container has been marked as deleted, all metadata will
            # have been erased so no point auditing. But we want it to pass, in
            # case any objects exist inside it.
            return True
        if broker.is_root_container():
            return self._audit_root_container(broker)
        return self._audit_shard_container(broker)

    def _update_root_container(self, broker):
        own_shard_range = broker.get_own_shard_range(no_default=True)
        if not own_shard_range:
            return

        # persist the reported shard metadata
        broker.merge_shard_ranges([own_shard_range])
        # now get a consistent list of own and other shard ranges
        shard_ranges = broker.get_shard_ranges(
            include_own=True,
            include_deleted=True)
        # send everything
        self._send_shard_ranges(
            broker.root_account, broker.root_container,
            shard_ranges)

    def _identify_sharding_candidate(self, broker, node):
        own_shard_range = broker.get_own_shard_range()
        if self._is_sharding_candidate(own_shard_range):
            try:
                file_size = os.stat(broker.db_file).st_size
            except OSError:
                file_size = None

            info = {'path': broker.db_file,
                    'node_index': node.get('index'),
                    'account': broker.account,
                    'container': broker.container,
                    'root': broker.root_path,
                    'object_count': own_shard_range.object_count,
                    'meta_timestamp': own_shard_range.meta_timestamp.internal,
                    'file_size': file_size}
            self._append_stat('sharding_candidates', 'all', info)

    def _process_broker(self, broker, node, part):
        # TODO: sigh, we should get the info cached *once*, somehow
        broker.get_info()  # make sure account/container are populated
        state = broker.get_db_state()
        self.logger.info('Starting processing %s/%s state %s',
                         broker.account, broker.container,
                         broker.get_db_state_text(state))

        if not self._audit_container(broker):
            return

        # now look and deal with misplaced objects.
        self._move_misplaced_objects(broker)

        if broker.is_deleted():
            # This container is deleted so we can skip it. We still want
            # deleted containers to go via misplaced items because they may
            # have new objects sitting in them that may need to move.
            return

        # TODO: bring back leader election (maybe?); if so make it
        # on-demand since we may not need to know if we are leader for all
        # states
        is_leader = node['index'] == 0 and self.auto_shard
        try:
            if state in (UNSHARDED, COLLAPSED):
                if is_leader and broker.is_root_container():
                    # bootstrap sharding of root container
                    self._find_sharding_candidates(
                        broker, shard_ranges=[broker.get_own_shard_range()])

                own_shard_range = broker.get_own_shard_range()
                if broker.get_shard_ranges():
                    # container may have been given shard ranges rather than
                    # found them e.g. via replication or a shrink event
                    if own_shard_range.state in (ShardRange.SHARDING,
                                                 ShardRange.SHRINKING,
                                                 ShardRange.SHARDED):
                        if broker.set_sharding_state():
                            state = SHARDING
                    else:
                        self.logger.debug(
                            'Shard ranges found but own shard range is %r: %s'
                            % (own_shard_range.state_text, broker.path))
                elif own_shard_range.state in (ShardRange.SHARDING,
                                               ShardRange.SHRINKING):
                    broker.update_sharding_info({'Scan-Done': 'False'})
                    if broker.set_sharding_state():
                        state = SHARDING

            if state == SHARDING:
                num_found = 0
                scan_complete = config_true_value(
                    broker.get_sharding_info('Scan-Done'))
                if is_leader and not scan_complete:
                    scan_complete, num_found = self._find_shard_ranges(broker)

                # create shard containers for newly found ranges
                num_created = self._create_shard_containers(broker)

                if num_found or num_created:
                    # share updated shard range state with  other nodes
                    # TODO: only replicate shard ranges, not objects
                    self._replicate_object(part, broker.db_file, node['id'])

                # always try to cleave any pending shard ranges
                cleave_complete = self._cleave(broker)

                if scan_complete and cleave_complete:
                    # This container has been completely cleaved. Move all
                    # CLEAVED shards to ACTIVE state and delete own shard
                    # range; these changes will be simultaneously reported in
                    # the next update to the root container.
                    self.logger.info('Completed cleaving of %s/%s',
                                     broker.account, broker.container)
                    modified_shard_ranges = broker.get_shard_ranges(
                        states=ShardRange.CLEAVED)
                    for sr in modified_shard_ranges:
                        sr.update_state(ShardRange.ACTIVE)
                    own_shard_range = broker.get_own_shard_range()
                    own_shard_range.update_state(ShardRange.SHARDED)
                    own_shard_range.update_meta(0, 0)
                    if (not broker.is_root_container() and not
                            own_shard_range.deleted):
                        own_shard_range = own_shard_range.copy(
                            timestamp=Timestamp.now(), deleted=1)
                    modified_shard_ranges.append(own_shard_range)
                    broker.merge_shard_ranges(modified_shard_ranges)

                    if broker.set_sharded_state():
                        state = SHARDED
                        self.logger.increment('sharding_complete')
                    else:
                        self.logger.debug('Remaining in sharding state %s/%s',
                                          broker.account, broker.container)

            if state == SHARDED and broker.is_root_container():
                if is_leader:
                    self._find_shrinks(broker)
                    self._find_sharding_candidates(broker)
                for shard_range in broker.get_shard_ranges(
                        states=[ShardRange.SHARDING]):
                    self._send_shard_ranges(
                        shard_range.account, shard_range.container,
                        [shard_range])

            if not broker.is_root_container():
                # Update the root container with this container's shard range
                # info; do this even when sharded in case previous attempts
                # failed; don't do this if there is no own shard range. When
                # sharding a shard, this is when the root will see the new
                # shards move to ACTIVE state and the sharded shard
                # simultaneously become deleted.
                # TODO: the new shard ranges may have existed for some
                # time and already be updating the root with their
                # usage e.g. multiple cycles before we finished
                # cleaving, or process failed right here. If so then
                # the updates we send here probably have an out of date
                # view of the shards' usage, but I think it is still ok
                # to send these updates because the meta_timestamp
                # should prevent these updates undoing any newer
                # updates at the root from the actual shards. *It would
                # be good to have a test to verify that.*
                self._update_root_container(broker)

            self.logger.info('Finished processing %s/%s state %s',
                             broker.account, broker.container,
                             broker.get_db_state_text())
        finally:
            self.logger.increment('scanned')

    def _check_node(self, node, devices_to_shard):
        if not node:
            return False
        if node['device'] not in devices_to_shard:
            return False
        if not is_local_device(self.ips, self.port,
                               node['replication_ip'],
                               node['replication_port']):
            return False
        if not check_drive(self.root, node['device'],
                           self.mount_check):
            self.logger.warning(
                'Skipping %(device)s as it is not mounted' % node)
            return False
        return True

    def _one_shard_cycle(self, devices_to_shard=None,
                         partitions_to_shard=None):
        """
        The main function, everything the sharder does forks from this method.

        The sharder loops through each container with sharding enabled and each
        sharded container on the server, on each container it:
            - audits the container
            - checks and deals with misplaced items
            - 2 phase sharding (if no. objects > shard_container_size)
                - Phase 1, if there is no shard defined, find it, then move
                  to next container.
                - Phase 2, if there is a shard defined, shard it.
            - Shrinking (check to see if we need to shrink this container).
        """
        self.logger.info('Container sharder cycle starting, auto-sharding %s',
                         self.auto_shard)
        self._zero_stats()
        if isinstance(devices_to_shard, (list, tuple)):
            self.logger.info('(Override devices: %s)',
                             ', '.join(str(d) for d in devices_to_shard))
        if isinstance(partitions_to_shard, (list, tuple)):
            self.logger.info('(Override partitions: %s)',
                             ', '.join(str(p) for p in partitions_to_shard))
        dirs = []
        self.ips = whataremyips(bind_ip=self.bind_ip)
        for node in self.ring.devs:
            if not self._check_node(node, devices_to_shard):
                continue
            datadir = os.path.join(self.root, node['device'], self.datadir)
            if os.path.isdir(datadir):
                # Populate self._local_device_ids so we can find
                # handoffs for shards later
                self._local_device_ids.add(node['id'])
                part_filt = self._partition_dir_filter(
                    node['id'],
                    partitions_to_shard)
                dirs.append((datadir, node['id'], part_filt))
        if not dirs:
            self.logger.warning('Found no data dirs!')
        for part, path, node_id in db_replicator.roundrobin_datadirs(dirs):
            # NB: get_part_nodes always provides an 'index' key
            for node in self.ring.get_part_nodes(int(part)):
                if node['id'] == node_id:
                    break
            else:
                # TODO: this would be a bug, a warning log may be too soft
                # tburke: or is it just a handoff? doesn't seem exceptional...
                self.logger.warning("Failed to find node to match id %s" %
                                    node_id)
                continue

            broker = ContainerBroker(path, logger=self.logger)
            try:
                self._identify_sharding_candidate(broker, node)
                if sharding_enabled(broker):
                    self._increment_stat('visited', 'attempted')
                    self._process_broker(broker, node, part)
                    self._increment_stat('visited', 'success')
                else:
                    self._increment_stat('visited', 'skipped')
            except Exception as err:
                self._increment_stat('visited', 'failure')
                self.logger.exception(
                    'Unhandled exception while processing %s: %s',
                    path, err)

            self._periodic_report_stats()

        self._report_stats()

    def _find_shard_ranges(self, broker):
        """
        Scans the container to find shard ranges and adds them to the shard
        ranges table. If there are existing shard ranges then scanning starts
        from the upper bound of the uppermost existing shard range.

        :param broker: An instance of :class:`swift.container.ContainerBroker`
        :return: a tuple of (success, num of shard ranges found) where success
            is True if the last shard range has been found, False otherwise.
        """
        self.logger.info('Started scan for shard ranges on %s/%s',
                         broker.account, broker.container)
        self._increment_stat('scanned', 'attempted')

        start = time.time()
        shard_data, last_found = broker.find_shard_ranges(
            self.split_size, limit=self.scanner_batch_size,
            existing_ranges=broker.get_shard_ranges())
        elapsed = time.time() - start

        if not shard_data:
            if last_found:
                self.logger.info("Already found all shard ranges")
                # set scan done in case it's missing
                broker.update_sharding_info({'Scan-Done': 'True'})
                self._increment_stat('scanned', 'success')
            else:
                # we didn't find anything
                self.logger.warning("No shard ranges found")
                self._increment_stat('scanned', 'failure')
            return last_found, 0

        # TODO: if we bring back leader election, this is about the spot where
        # we should confirm we're still the scanner
        shard_ranges = make_shard_ranges(
            broker, shard_data, self.shards_account_prefix)
        broker.merge_shard_ranges(shard_ranges)
        num_found = len(shard_ranges)
        self.logger.info(
            "Completed scan for shard ranges: %d found", num_found)
        self._increment_stat('scanned', 'found', step=num_found)
        self._min_stat('scanned', 'min_time', round(elapsed / num_found, 3))
        self._max_stat('scanned', 'max_time', round(elapsed / num_found, 3))

        if last_found:
            # We've found the last shard range, so mark that in metadata
            broker.update_sharding_info({'Scan-Done': 'True'})
            self.logger.info("Final shard range reached.")
        self._increment_stat('scanned', 'success')
        return last_found, num_found

    def _create_shard_containers(self, broker):
        # Create shard containers that are ready to receive redirected object
        # updates. Do this now, so that redirection can begin immediately
        # without waiting for cleaving to complete.
        found_ranges = broker.get_shard_ranges(states=ShardRange.FOUND)
        created_ranges = []
        for shard_range in found_ranges:
            self._increment_stat('created', 'attempted')
            shard_range.update_state(ShardRange.CREATED)
            headers = {
                'X-Backend-Storage-Policy-Index': broker.storage_policy_index,
                'X-Container-Sysmeta-Shard-Root': broker.root_path,
                'X-Container-Sysmeta-Sharding': True}
            success = self._send_shard_ranges(
                shard_range.account, shard_range.container,
                [shard_range], headers=headers)
            if success:
                self.logger.debug('PUT new shard range container for %s',
                                  shard_range)
                self._increment_stat('created', 'success')
            else:
                self.logger.error(
                    'PUT of new shard container %r failed for %s.',
                    shard_range, broker.path)
                self._increment_stat('created', 'failure')
                # break, not continue, because elsewhere it is assumed that
                # finding and cleaving shard ranges progresses linearly, so we
                # do not want any subsequent shard ranges to be in created
                # state while this one is still in found state
                break
            created_ranges.append(shard_range)
            self.logger.increment('shard_ranges_created')

        broker.merge_shard_ranges(created_ranges)
        if not broker.is_root_container():
            # TODO: check for success and do not proceed otherwise
            self._send_shard_ranges(
                broker.root_account, broker.root_container, created_ranges)
        self.logger.info(
            "Completed creating shard range containers: %d created.",
            len(created_ranges))
        return len(created_ranges)

    def _is_sharding_candidate(self, shard_range):
        return (shard_range.state == ShardRange.ACTIVE and
                shard_range.object_count >= self.shard_container_size)

    def _find_sharding_candidates(self, broker, shard_ranges=None):
        # this should only execute on root containers; the goal is to find
        # large shard containers that should be sharded.
        # First cut is simple: assume root container shard usage stats are good
        # enough to make decision.
        # TODO: object counts may well not be the appropriate metric for
        # deciding to shrink because a shard with low object_count may have a
        # large number of deleted object rows that will need to be merged with
        # a neighbour. We may need to expose row count as well as object count.
        if shard_ranges is None:
            shard_ranges = broker.get_shard_ranges(states=[ShardRange.ACTIVE])
        candidates = []
        for shard_range in shard_ranges:
            if not self._is_sharding_candidate(shard_range):
                continue
            shard_range.update_state(ShardRange.SHARDING,
                                     state_timestamp=Timestamp.now())
            shard_range.epoch = shard_range.state_timestamp
            candidates.append(shard_range)
        broker.merge_shard_ranges(candidates)
        return len(candidates)

    def _find_shrinks(self, broker):
        # this should only execute on root containers that have sharded; the
        # goal is to find small shard containers that could be retired by
        # merging with a neighbour.
        # First cut is simple: assume root container shard usage stats are good
        # enough to make decision; only merge with upper neighbour so that
        # upper bounds never change (shard names include upper bound).
        # TODO: object counts may well not be the appropriate metric for
        # deciding to shrink because a shard with low object_count may have a
        # large number of deleted object rows that will need to be merged with
        # a neighbour. We may need to expose row count as well as object count.
        state = broker.get_db_state()
        if state != SHARDED:
            self.logger.warning(
                'Cannot shrink a not yet sharded container %s/%s',
                broker.account, broker.container)

        shard_ranges = broker.get_shard_ranges()
        own_shard_range = broker.get_own_shard_range()
        if len(shard_ranges) == 1:
            # special case to enable final shard to shrink into root
            shard_ranges.append(own_shard_range)

        merge_pairs = {}
        for donor, acceptor in zip(shard_ranges, shard_ranges[1:]):
            self.logger.debug('considering shrink donor    %r %s' %
                              (donor, donor.name))
            self.logger.debug('considering shrink acceptor %r %s' %
                              (acceptor, acceptor.name))
            if donor in merge_pairs:
                # this range may already have been made an acceptor; if so then
                # move on. In principle it might be that even after expansion
                # this range and its donor(s) could all be merged with the next
                # range. In practice it is much easier to reason about a single
                # donor merging into a single acceptor. Don't fret - eventually
                # all the small ranges will be retired.
                continue
            if (acceptor is not own_shard_range and
                    acceptor.state != ShardRange.ACTIVE):
                # don't shrink into a range that is not yet ACTIVE
                continue
            if donor.state not in (ShardRange.ACTIVE, ShardRange.SHRINKING):
                # found? created? sharded? don't touch it
                continue

            proposed_object_count = donor.object_count + acceptor.object_count
            if (donor.state == ShardRange.SHRINKING or
                    (donor.object_count < self.shrink_size and
                     proposed_object_count < self.merge_size)):
                # include previously identified merge pairs on presumption that
                # following shrink procedure is idempotent
                merge_pairs[acceptor] = donor
                self.logger.debug('selecting shrink pair %r %r' %
                                  (donor, acceptor))

        # TODO: think long and hard about the correct order for these remaining
        # operations and what happens when one fails...
        for acceptor, donor in merge_pairs.items():
            # TODO: unit test to verify idempotent nature of this procedure
            self.logger.debug('shrinking shard range %s into %s in %s' %
                              (donor, acceptor, broker.db_file))
            modified_shard_ranges = []
            if donor.update_state(ShardRange.SHRINKING):
                # Set donor state to shrinking so that next cycle won't use it
                # as an acceptor; state_timestamp defines new epoch for donor
                # and new timestamp for the expanded acceptor below.
                donor.epoch = donor.state_timestamp = Timestamp.now()
                modified_shard_ranges.append(donor)
            if acceptor.lower != donor.lower:
                # Update the acceptor container with its expanding state to
                # prevent it treating objects cleaved from the donor
                # as misplaced.
                acceptor.lower = donor.lower
                acceptor.timestamp = donor.state_timestamp
                modified_shard_ranges.append(acceptor)
            broker.merge_shard_ranges(modified_shard_ranges)
            if acceptor is not own_shard_range:
                self._send_shard_ranges(
                    acceptor.account, acceptor.container, [acceptor])
                acceptor.increment_meta(donor.object_count, donor.bytes_used)
            else:
                # no need to change namespace or stats
                acceptor.update_state(ShardRange.ACTIVE,
                                      state_timestamp=Timestamp.now())
            # Set Scan-Done so that the donor will not scan itself and will
            # transition to SHARDED state once it has cleaved to the acceptor;
            # TODO: if the PUT request successfully write the Scan-Done sysmeta
            # but fails to write the acceptor shard range, then the shard is
            # left with Scan-Done set, and if it was to then grow in size to
            # eventually need sharding, we don't want Scan-Done prematurely
            # set. This is an argument for basing 'scan done condition' on the
            # existence of a shard range whose upper >= shard own range, rather
            # than using sysmeta Scan-Done.
            headers = {'X-Container-Sysmeta-Shard-Scan-Done': True}
            # Now send a copy of the expanded acceptor, with an updated
            # timestamp, to the donor container. This forces the donor to
            # asynchronously cleave its entire contents to the acceptor. The
            # donor will pass a deleted copy of its own shard range and the
            # newer expended acceptor shard range to the acceptor when
            # cleaving. Subsequent updates from the acceptor will then update
            # the root to have the expanded acceptor namespace and deleted
            # donor shard range. Once cleaved, the donor will also update the
            # root directly with its deleted own shard range and the expanded
            # acceptor shard range.
            self._send_shard_ranges(donor.account, donor.container,
                                    [donor, acceptor], headers=headers)

    def _cleave(self, broker):
        # Returns True if misplaced objects have been moved and all *available*
        # shard ranges have been successfully cleaved, False otherwise
        state = broker.get_db_state()
        if state == SHARDED:
            self.logger.debug('Passing over already sharded container %s/%s',
                              broker.account, broker.container)
            return True

        cleave_context = broker.load_cleave_context()
        misplaced_done = cleave_context.get('misplaced_done', False)
        if not misplaced_done:
            # ensure any misplaced objects in the source broker are moved; note
            # that this invocation of _move_misplaced_objects is targetted at
            # the *retiring* db.
            self.logger.debug(
                'Moving any misplaced objects from sharding container: %s',
                broker.path)
            bounds = self._make_default_misplaced_object_bounds(broker)
            misplaced_done = self._move_misplaced_objects(
                broker, src_broker=broker.get_brokers()[0],
                src_bounds=bounds)
            cleave_context['misplaced_done'] = misplaced_done
            broker.dump_cleave_context(cleave_context)

        if cleave_context.get('cleaving_done', False):
            self.logger.debug('Cleaving already complete for container %s',
                              broker.path)
            return misplaced_done

        cleave_cursor = cleave_context.get('cursor') or ''
        ranges_todo = broker.get_shard_ranges(
            marker=cleave_cursor + '\x00',
            states=[ShardRange.CREATED, ShardRange.CLEAVED, ShardRange.ACTIVE])
        if not ranges_todo:
            self.logger.debug('No shard ranges to cleave for %s', broker.path)
            return misplaced_done

        self.logger.debug(
            '%s to cleave %s', 'Continuing' if cleave_cursor else 'Starting',
            broker.path)

        own_shard_range = broker.get_own_shard_range()
        ranges_done = []
        policy_index = broker.storage_policy_index
        brokers = broker.get_brokers()
        for shard_range in ranges_todo[:self.shard_batch_size]:
            self.logger.info(
                "Cleaving '%s': %r", broker.path, shard_range)
            self._increment_stat('cleaved', 'attempted')
            start = time.time()
            try:
                # use force here because we may want to update existing shard
                # metadata timestamps
                shard_part, shard_broker, node_id = self._get_shard_broker(
                    shard_range, broker.root_path, policy_index, force=True)
            except DeviceUnavailable as duex:
                self.logger.warning(str(duex))
                self.logger.increment('failure')
                self._increment_stat('cleaved', 'failure')
                return False

            with shard_broker.sharding_lock():
                for source_broker in brokers:
                    for objects, info in self.yield_objects(
                            source_broker, shard_range, policy_index):
                        shard_broker.merge_items(objects)

            if shard_range.includes(own_shard_range):
                # When shrinking, include deleted own (donor) shard range in
                # the replicated db so that when acceptor next updates root it
                # will atomically update its namespace *and* delete the donor.
                # Don't do this when sharding a shard because the donor
                # namespace should not be deleted until all shards are cleaved.
                if own_shard_range.update_state(ShardRange.SHARDED):
                    own_shard_range.set_deleted()
                    broker.merge_shard_ranges([own_shard_range])
                shard_broker.merge_shard_ranges([own_shard_range])
            elif shard_range.state == ShardRange.CREATED:
                # The shard range object stats may have changed since the shard
                # range was found, so update with stats of objects actually
                # copied to the shard broker. Only do this the first time each
                # shard range is cleaved.
                info = shard_broker.get_info()
                shard_range.update_meta(
                    info['object_count'], info['bytes_used'])
                shard_range.update_state(ShardRange.CLEAVED)
                shard_broker.merge_shard_ranges([shard_range])

            self.logger.info(
                'Replicating new shard container %s for %s',
                shard_broker.path, shard_broker.get_own_shard_range())

            success, responses = self._replicate_object(
                shard_part, shard_broker.db_file, node_id)

            quorum = quorum_size(self.ring.replica_count)
            if not success and responses.count(True) < quorum:
                # break because we don't want to progress the cleave cursor
                # until each shard range has been successfully cleaved
                self.logger.warning(
                    'Failed to sufficiently replicate cleaved shard %s for %s',
                    shard_range, broker.path)
                break

            ranges_done.append(shard_range)

            self.logger.info(
                'Cleaved %s for shard range %s.', broker.path, shard_range)
            self.logger.increment('cleaved')
            self._increment_stat('cleaved', 'success')
            elapsed = round(time.time() - start, 3)
            self._min_stat('cleaved', 'min_time', elapsed)
            self._max_stat('cleaved', 'max_time', elapsed)

        if ranges_done:
            broker.merge_shard_ranges(ranges_done)
            cleave_context['cursor'] = str(ranges_done[-1].upper)
            if ranges_done[-1].upper >= own_shard_range.upper:
                # cleaving complete
                cleave_context['cleaving_done'] = True
            broker.dump_cleave_context(cleave_context)
        else:
            self.logger.warning('No cleaving progress made: %s', broker.path)

        return misplaced_done and (len(ranges_done) == len(ranges_todo))

    def run_forever(self, *args, **kwargs):
        """Run the container sharder until stopped."""
        self.reported = time.time()
        time.sleep(random() * self.interval)
        while True:
            begin = time.time()
            try:
                self._one_shard_cycle(devices_to_shard=Everything(),
                                      partitions_to_shard=Everything())
            except (Exception, Timeout):
                self.logger.increment('errors')
                self.logger.exception('Exception in sharder')
            elapsed = time.time() - begin
            self.logger.info(
                'Container sharder cycle completed: %.02fs', elapsed)
            if elapsed < self.interval:
                time.sleep(self.interval - elapsed)

    def run_once(self, *args, **kwargs):
        """Run the container sharder once."""
        self.logger.info('Begin container sharder "once" mode')
        devices_to_shard, partitions_to_shard = parse_overrides(
            **kwargs)
        begin = self.reported = time.time()
        self._one_shard_cycle(devices_to_shard=devices_to_shard,
                              partitions_to_shard=partitions_to_shard)
        elapsed = time.time() - begin
        self.logger.info(
            'Container sharder "once" mode completed: %.02fs', elapsed)
