# -*- coding: utf-8 -*-
from functools import wraps
from itertools import imap
import json
import random
import traceback
import threading

from cocaine.logging import Logger
import elliptics
import timed_queue

from config import config
import storage
from cache_transport.transport import transport


logging = Logger()

__update_lock = threading.Lock()


def update_lock(func):

    @wraps(func)
    def wrapped(*args, **kwargs):
        with __update_lock:
            logging.info('Lock acquired')
            return func(*args, **kwargs)

    return wrapped


class CacheManager(object):

    ITEM_SIZE_KEY = 'size'

    def __init__(self, session, index_prefix):
        self.__session = session

        self.__index_prefix = index_prefix
        self.__namespaces = {}

        # bandwidth settings
        self.__base_bw_per_instance = 50 * 1024 * 1024  # 50 Mb/sec
        self.__bw_per_instance = self.__base_bw_per_instance
        self.__bw_degradation_threshold = 5

        self.__tq = timed_queue.TimedQueue()
        self.__tq.start()

        self.__tq.add_task_in('cache_status_update', 10, self.cache_status_update)
        self.__tq.add_task_in('cache_list_update', 15, self.update_cache_list)

        self.keys = {}
        self.instances = {}

    def add_namespace(self, namespace):
        total_space = sum(map(lambda g: g.get_stat().total_space, self.cache_groups()))
        self.__namespaces.setdefault(namespace, {'total_space': total_space,
                                                 'cache_size': 0.0})
        self.keys[namespace] = {}

    def __loads(self, item):
        try:
            return json.loads(item.indexes[0].data)
        except Exception as e:
            logging.info('Failed to load cache item: %s' % e)
            return None

    @update_lock
    def update_cache_list(self):
        try:
            logging.info('Updating cache list')

            if not self.instances:
                logging.info('Cache instances are not yet fetched')
                return

            indexes = [(self.__index_prefix + ns).encode('utf-8') for ns in self.__namespaces]

            self.__sync(imap(self.__loads, self.__session.find_any_indexes(indexes)))

        except Exception as e:
            logging.error("Error while updating cache list: %s\n%s" % (str(e), traceback.format_exc()))
        finally:
            cache_list_update_period = config['cache'].get('list_update_period', 30)
            self.__tq.add_task_in('cache_list_update', cache_list_update_period, self.update_cache_list)
            logging.info('Cache list updated')

    @update_lock
    def upload_list(self, request):
        data = request['request']
        logging.info('request: %s ' % request)
        files = json.loads(data['files'])
        ns = data['namespace']

        if not ns in self.__namespaces:
            logging.info('Invalid cache namespace: %s' % ns)

        logging.info('Files to upload: %s' % (files,))

        files = sorted(files, key=lambda f: f['traffic'], reverse=True)

        self.__sync(files, namespace=ns, with_update=True)

        return 'processed'

    def __sync(self, items, namespace=None, with_update=False):

        keys_to_remove = {}

        if namespace and namespace in self.__namespaces:
            namespaces = [namespace]
        else:
            namespaces = self.__namespaces

        if not namespaces:
            logging.info('No valid namespaces for synchronizing cache')
            return

        for ns in namespaces:
            keys_to_remove[ns] = set(self.keys[ns].keys())

        for item in items:
            logging.info('Updating cache key %s' % item['key'])

            ns = namespace or item.get('namespace')
            if not ns:
                logging.info('No namespace for key %s' % item['key'])
                continue

            keys_to_remove[ns].discard(item['key'])

            existing_key = self.keys[ns].get(item['key'])

            ext_groups = set(item.get('dgroups') or [])
            cur_groups = set(existing_key and existing_key['dgroups'] or [])

            if existing_key:
                logging.info('Existing key: %s' % item['key'])

            if with_update:
                req_ci_num = self.__cache_instances_num(item['traffic'])

                req_ci_num -= len(cur_groups)
                logging.info('Key %s already dispatched %s cache instances, %s more required' % (item['key'], len(cur_groups), req_ci_num))

                if req_ci_num == 0:
                    ext_groups = cur_groups
                elif req_ci_num < 0:
                    cis = self.__cis_choose_remove(abs(req_ci_num), existing_key['dgroups'])

                    gids = set([ci.group.group_id for ci in cis])
                    ext_groups = cur_groups - gids
                    # TODO: maybe move to outer level
                    existing_key['dgroups'] = list(ext_groups)

                    task = __transport_key(existing_key, action='remove', dgroups=list(gids))
                    transport.put(json.dumps(task))

                    self.__upstream_update_key(ns, existing_key)
                else:
                    space_needed = self.__need_space(ns, req_ci_num, item[self.ITEM_SIZE_KEY])
                    if space_needed:
                        freed_space = self.__pop_least_popular_keys(self, ns, item['traffic'], space_needed)
                        if freed_space < space_needed:
                            logging.info('Not enough space for key %s (size: %s, require add.space: %s kb)' % (item['key'], item[self.ITEM_SIZE_KEY] / 1024.0, space_needed / 1024.0))
                            continue

                    cis = self.__cis_choose_add(req_ci_num, item['sgroups'], item['traffic'], item[self.ITEM_SIZE_KEY])

                    key = {}
                    for k in ('key', self.ITEM_SIZE_KEY, 'traffic', 'sgroups'):
                        key[k] = item[k]

                    updated_key = self.keys[ns].setdefault(key['key'], {'dgroups': []})
                    updated_key.update(key)
                    ext_groups = set(updated_key['dgroups'] + [ci.group.group_id for ci in cis])
                    updated_key['dgroups'] = list(ext_groups)
                    updated_key['namespace'] = ns

                    # TODO: exclude existing dgroups from task
                    task = self.__transport_key(updated_key, action='add')
                    logging.info('Put task for cache distribution: %s' % task)
                    transport.put(json.dumps(task))

                    self.__upstream_update_key(ns, updated_key)

            if not with_update:
                self.keys[ns][item['key']] = item

            for gid in cur_groups - ext_groups:
                group = storage.groups[gid]
                if not group in self.instances:
                    continue
                self.instances[group].remove_file(item[self.ITEM_SIZE_KEY])
            for gid in ext_groups - cur_groups:
                group = storage.groups[gid]
                if not group in self.instances:
                    continue
                self.instances[group].add_file(item[self.ITEM_SIZE_KEY])

            # ns_cache_sizes[namespace] += item[self.ITEM_SIZE_KEY] * len(ext_groups)

        for ns in namespaces:
            for key in keys_to_remove[ns]:
                existing_key = self.keys[ns][key]

                if with_update:
                    task = self.__transport_key(existing_key, action='remove', dgroups=existing_key['dgroups'])
                    transport.put(json.dumps(task))
                    self.__upstream_remove_key(ns, existing_key)

                for gid in existing_key['dgroups']:
                    group = storage.groups[gid]
                    self.instances[group].remove_file(item[self.ITEM_SIZE_KEY])

                del self.keys[ns][key]

        # for ns, ns_stat in self.__namespaces.iteritems():
        #     ns_stat['cache_size'] = ns_cache_sizes[ns]
        #     logging.info('Cache size for cache namespace %s: %s kb' % (ns, ns_stat['cache_size'] / 1024.0))

    def __upstream_update_key(self, namespace, key):
        key_ = key['key']
        if isinstance(key_, unicode):
            key_ = key_.encode('utf-8')
        eid = elliptics.Id(key_)
        index_name = self.__index_prefix + namespace
        self.__session.update_indexes(eid, [index_name.encode('utf-8')], [json.dumps(key)]).get()

    def __upstream_remove_key(self, namespace, key):
        key_ = key['key']
        if isinstance(key_, unicode):
            key_ = key_.encode('utf-8')
        eid = elliptics.Id(key_)
        updated_indexes = []
        updated_datas = []
        for ns in self.__namespaces:
            if ns == namespace:
                continue
            if key['key'] in self.keys[ns]:
                updated_indexes.append((self.__index_prefix + ns).encode('utf-8'))
                updated_datas.append(json.dumps(self.keys[ns][key['key']]))
        self.__session.set_indexes(eid, updated_indexes, updated_datas)

    def get_cached_keys(self, request):
        res = []
        for ns_keys in self.keys.itervalues():
            for key, item in ns_keys.iteritems():
                groups = item['dgroups']
                res.append((key, tuple(groups)))
        return res

    def get_cached_keys_by_group(self, request):

        group_id = int(request)

        res = []

        indexes = [(self.__index_prefix + ns).encode('utf-8') for ns in self.__namespaces]
        for item in self.__session.find_any_indexes(indexes):
            try:
                item = json.loads(item.indexes[0].data)
            except Exception as e:
                logging.info('Failed to load cache item: %s' % e)
                continue

            if group_id in item['dgroups']:
                res.append(self.__transport_key(item))

        return res

    def __transport_key(self, key, action=None, dgroups=None):
        task = {
            'key': key['key'],
            'dgroups': dgroups if dgroups else list(key['dgroups']),
            'sgroups': [] if action == 'remove' else list(key['sgroups']),
        }
        if action:
            task['action'] = action
        return task

    def __need_space(self, namespace, req_ci_num, filesize):
        ns_stat = self.__namespaces[namespace]
        return (ns_stat['total_space'] - ns_stat['cache_size']) > (filesize * req_ci_num)

    def __clear_ns_space(self, namespace, cis, key):
        space = len(cis) * key[self.ITEM_SIZE_KEY]
        self.__namespaces[namespace]['cache_size'] -= space
        return space

    def __pop_least_popular_keys(self, namespace, ts_traffic, space_needed=0):
        sorted_keys = sorted(self.keys[namespace], key=lambda k: k['traffic'], reverse=True)
        l_key, sorted_keys = sorted_keys[-1], sorted_keys[:-1]

        freed_space = 0

        while freed_space < space_needed and ts_traffic > lkey['traffic']:

            # create task on removal

            del self.keys[namespace][l_key['key']]

            space = self.__clear_ns_space(l_key['dgroups'], l_key)
            for gid in l_key['dgroups']:
                self.instances[gid].remove_file(l_key[self.ITEM_SIZE_KEY])

            freed_space += space
            l_key, sorted_keys = sorted_keys[-1], sorted_keys[:-1]

        return freed_space


    def __bandwidth_degrade(self, la):
        """Returns the performance degradation coefficient"""
        la_ = min(la, 10)
        return 1.0 - ((la_ - self.__bw_degradation_threshold) / (10 - self.__bw_degradation_threshold)) ** 1.5

    def cache_groups(self):
        couples = filter(lambda c: c.namespace == 'cache', storage.couples)
        return set([g for c in couples for g in c.groups])

    def cache_status_update(self):
        try:
            groups = self.cache_groups()
            new_groups = [g for g in groups if g not in self.instances]
            for g in new_groups:
                logging.info('Adding new cache instance (group %s)' % g)
                ci = CacheInstance(g)
                self.instances[ci] = ci


            logging.info('Current cache instances: %s' % self.instances.keys())

            las = sorted([c.load_average for c in self.instances])
            logging.info(las)
            median = las[len(las) / 2]
            logging.info('Current LA median: %s' % median)

            self.__bw_per_instance = (self.__base_bw_per_instance
                                      if median <= self.__bw_degradation_threshold else
                                      self.__bandwidth_degrade(median) * self.__base_bw_per_instance)
            logging.info('Node bandwidth was set to %s Mbytes/sec' % self.__bw_per_instance)
        except Exception as e:
            logging.error("Error while updating cache bandwidth: %s\n%s" % (str(e), traceback.format_exc()))
        finally:
            cache_status_update_period = config['cache'].get('status_update_period', 60)
            self.__tq.add_task_in('bandwidth_update', cache_status_update_period, self.cache_status_update)


    def __cache_instances_num(self, traffic):
        return min(len(self.instances), int(traffic / self.__bw_per_instance) + 1)

    def __cache_instances_rnd_choice(self, cis, num):
        # bad algorithm: weights sum is calculated for every cache instance
        # separately
        choice = []
        for i in xrange(num):
            idx = 0
            weights = [ci.weight for ci in cis]
            weights_sum = sum(weights)
            logging.info('Cache instances weights: %s' % weights)
            rnd = random.random() * weights_sum
            for i, w in enumerate(weights):
                rnd -= w
                if rnd < 0:
                    idx = i
                    break
            choice.append(cis[idx])
            del cis[idx]
        return choice


    def __cis_choose_add(self, req_num, sgroups, traffic, filesize):
        cis = self.instances.keys()

        shosts = set()
        for g in sgroups:
            for n in storage.groups[g].nodes:
                shosts.add(n)

        logging.info('Source hosts: %s' % shosts)

        filtered_cis = []
        for ci in cis:
            logging.info('Checking cache instance %s' % (ci))
            try:
                # FOR LOCAL TESTING
                # for n in g:
                #     if n.host in shosts:
                #         raise ValueError('Host of a couple matches the key source host: %s' % n.host)
                if ci.group.group_id in sgroups:
                    raise ValueError('Cache instance %s is already in source groups' % ci)

                if ci.free_space < filesize:
                    raise ValueError('Cache instance %s has not enough free space' % ci)

                if ci.status != storage.Status.COUPLED:
                    raise ValueError('Cache instance %s group status is not COUPLED' % ci)

            except ValueError as e:
                logging.info("Can't use cache instance %s: %s" % (ci, e))
                continue
            else:
                filtered_cis.append(ci)

        logging.info('Filtered cache instances with suitable hosts: %s' % filtered_cis)

        logging.info('Number of cache instances calculated: %s' % req_num)
        actual_num = min(req_num, len(filtered_cis))
        logging.info('Number of cache instances to be used: %s' % actual_num)

        selected_cis = self.__cache_instances_rnd_choice(filtered_cis, actual_num)
        logging.info('Selected cache instances: %s' % selected_cis)

        return selected_cis

    def __cis_choose_remove(self, req_num, dgroups):
        cis = [self.instances[storage.groups[g]] for g in dgroups]
        return sorted(cis, key=lambda ci: ci.weight, reverse=True)[:req_num]


class CacheInstance(object):

    LA_THRESHOLD = 10.0
    MAX_LA = 10.0

    def __init__(self, group):
        self.group = group
        self.total_space = group.get_stat().total_space
        self.cache_size = 0

    @property
    def free_space(self):
        return self.total_space - self.cache_size

    @property
    def load_average(self):
        return min(self.group.get_stat().load_average, self.MAX_LA)

    @property
    def status(self):
        return self.group.status

    @property
    def weight(self):
        return (2 ** (1 - (self.load_average / self.LA_THRESHOLD)) +
                (self.free_space / self.total_space))

    def add_file(self, filesize):
        self.cache_size += filesize
        logging.info('Added file to cache instance %s: +%s kb = %s kb' % (
                     self, filesize / 1024.0, self.cache_size / 1024.0))

    def remove_file(self, filesize):
        self.cache_size = max(self.cache_size - filesize, 0)
        logging.info('Removed file from cache instance %s: -%s kb = %s kb' % (
                     self, filesize / 1024.0, self.cache_size / 1024.0))

    def __eq__(self, other):
        if isinstance(other, storage.Group):
            return self.group == other
        elif isinstance(other, CacheInstance):
            return self.group == other.group
        elif isinstance(other, int):
            return self.group.group_id == other
        return False

    def __hash__(self):
        return hash(self.group)

    def __str__(self):
        return str(self.group.group_id)

    def __repr__(self):
        return '<CacheInstance: group=[%s]>' % self.group
