# -*- coding: utf-8 -*-
from gevent import monkey
monkey.patch_all()

import logging
import logging.config
import os

from datetime import datetime
from time import time

from gevent import Greenlet, sleep
from gevent.queue import Empty
from iso8601 import parse_date
from pytz import timezone
from requests.exceptions import ConnectionError
from zope.interface import implementer

from openprocurement_client.exceptions import (
    InvalidResponse,
    RequestFailed,
    ResourceNotFound,
    ResourceGone
)
from openprocurement.bridge.basic.constants import PROCUREMENT_METHOD_TYPE_HANDLERS as handlers_registry
from openprocurement.bridge.basic.interfaces import IWorker
from openprocurement.bridge.basic.utils import journal_context


logger = logging.getLogger(__name__)
INFINITY = True
TZ = timezone(os.environ['TZ'] if 'TZ' in os.environ else 'Europe/Kiev')


@implementer(IWorker)
class BasicResourceItemWorker(Greenlet):

    def __init__(self, api_clients_queue=None, resource_items_queue=None,
                 db=None, config_dict=None, retry_resource_items_queue=None,
                 api_clients_info=None):
        Greenlet.__init__(self)
        self.exit = False
        self.update_doc = False
        self.db = db
        self.config = config_dict['worker_config']
        self.resource = config_dict['resource']
        self.api_clients_queue = api_clients_queue
        self.resource_items_queue = resource_items_queue
        self.retry_resource_items_queue = retry_resource_items_queue
        self.bulk = {}
        self.priority_cache = {}
        self.bulk_save_limit = self.config['bulk_save_limit']
        self.bulk_save_interval = self.config['bulk_save_interval']
        self.start_time = datetime.now()
        self.api_clients_info = api_clients_info

    def add_to_retry_queue(self, resource_item_id, priority=0, status_code=0):
        retries_count = priority - 1000 if priority >= 1000 else priority
        if retries_count > self.config['retries_count'] and status_code != 429:
            logger.critical(
                '{} {} reached limit retries count {} and droped from retry_queue.'.format(
                    self.resource[:-1].title(), resource_item_id, self.config['retries_count']),
                extra={'MESSAGE_ID': 'dropped_documents'}
            )
            return
        timeout = 0
        if status_code != 429:
            timeout = self.config['retry_default_timeout'] * retries_count
            priority += 1
        sleep(timeout)
        self.retry_resource_items_queue.put((priority, resource_item_id))
        logger.info('Put to \'retry_queue\' {}: {}'.format(self.resource[:-1], resource_item_id),
                    extra={'MESSAGE_ID': 'add_to_retry'})

    def _get_api_client_dict(self):
        if not self.api_clients_queue.empty():
            try:
                api_client_dict = self.api_clients_queue.get(timeout=self.config['queue_timeout'])
            except Empty:
                return None
            if self.api_clients_info[api_client_dict['id']]['drop_cookies']:
                try:
                    api_client_dict['client'].renew_cookies()
                    self.api_clients_info[api_client_dict['id']] = {
                        'drop_cookies': False,
                        'request_durations': {},
                        'request_interval': 0,
                        'avg_duration': 0
                    }
                    api_client_dict['request_interval'] = 0
                    api_client_dict['not_actual_count'] = 0
                    logger.info('Drop lazy api_client {} cookies'.format(api_client_dict['id']))
                except (Exception, ConnectionError) as e:
                    self.api_clients_queue.put(api_client_dict)
                    logger.debug(
                        'PUT API CLIENT: {}'.format(api_client_dict['id']), extra={'MESSAGE_ID': 'put_client'})
                    logger.error('While renewing cookies catch exception: {}'.format(e.message))
                    return None
            logger.debug(
                'GET API CLIENT: {} {} with requests interval: {}'.format(
                    api_client_dict['id'],
                    api_client_dict['client'].session.headers['User-Agent'],
                    api_client_dict['request_interval']
                ),
                extra={
                    'MESSAGE_ID': 'get_client',
                    'REQUESTS_TIMEOUT': api_client_dict['request_interval']
                }
            )
            sleep(api_client_dict['request_interval'])
            return api_client_dict
        else:
            return None

    def _get_resource_item_from_queue(self):
        priority = resource_item_id = None
        if not self.resource_items_queue.empty():
            priority, resource_item_id = self.resource_items_queue.get(timeout=self.config['queue_timeout'])
            logger.debug('Get {} {} from main queue.'.format(self.resource[:-1], resource_item_id))
        return priority, resource_item_id

    def _get_resource_item_from_public(self, api_client_dict, priority, resource_item_id):
        try:
            logger.debug('Request interval {} sec. for client {}'.format(
                api_client_dict['request_interval'], api_client_dict['client'].session.headers['User-Agent']),
                extra={'REQUESTS_TIMEOUT': api_client_dict['request_interval']})
            start = time()
            public_resource_item = api_client_dict['client'].get_resource_item(resource_item_id).get('data')
            self.api_clients_info[api_client_dict['id']]['request_durations'][datetime.now()] = time() - start
            self.api_clients_info[api_client_dict['id']]['request_interval'] = api_client_dict['request_interval']
            logger.debug(
                'Recieved from API {}: {} {}'.format(self.resource[:-1], public_resource_item['id'],
                                                     public_resource_item['dateModified'])
            )
            if api_client_dict['request_interval'] > 0:
                api_client_dict['request_interval'] -= self.config['client_dec_step_timeout']
            self.api_clients_queue.put(api_client_dict)
            logger.debug('PUT API CLIENT: {}'.format(api_client_dict['id']), extra={'MESSAGE_ID': 'put_client'})
            return public_resource_item
        except ResourceGone:
            self.api_clients_queue.put(api_client_dict)
            logger.debug('PUT API CLIENT: {}'.format(api_client_dict['id']), extra={'MESSAGE_ID': 'put_client'})
            logger.info('{} {} archived.'.format(self.resource[:-1].title(), resource_item_id))
            return None  # Archived
        except InvalidResponse as e:
            self.api_clients_info[api_client_dict['id']]['request_durations'][datetime.now()] = time() - start
            self.api_clients_info[api_client_dict['id']]['request_interval'] = api_client_dict['request_interval']
            self.api_clients_queue.put(api_client_dict)
            logger.debug('PUT API CLIENT: {}'.format(api_client_dict['id']), extra={'MESSAGE_ID': 'put_client'})
            logger.error(
                'Error while getting {} {} from public with status code: {}'.format(
                    self.resource[:-1], resource_item_id, e.status_code),
                extra={'MESSAGE_ID': 'exceptions'})
            self.add_to_retry_queue(resource_item_id, priority=priority)
            return None
        except RequestFailed as e:
            self.api_clients_info[api_client_dict['id']]['request_durations'][datetime.now()] = time() - start
            self.api_clients_info[api_client_dict['id']]['request_interval'] = api_client_dict['request_interval']
            if e.status_code == 429:
                if api_client_dict['request_interval'] > self.config['drop_threshold_client_cookies']:
                    api_client_dict['client'].session.cookies.clear()
                    api_client_dict['request_interval'] = 0
                else:
                    api_client_dict['request_interval'] += self.config['client_inc_step_timeout']
                self.api_clients_queue.put(api_client_dict, timeout=api_client_dict['request_interval'])
                logger.warning('PUT API CLIENT: {} after {} sec.'.format(api_client_dict['id'],
                                                                         api_client_dict['request_interval']),
                               extra={'MESSAGE_ID': 'put_client'})
            else:
                self.api_clients_queue.put(api_client_dict)
                logger.debug('PUT API CLIENT: {}'.format(api_client_dict['id']), extra={'MESSAGE_ID': 'put_client'})
            logger.error(
                'Request failed while getting {} {} from public with status code {}: '.format(
                    self.resource[:-1], resource_item_id, e.status_code),
                extra={'MESSAGE_ID': 'exceptions'})
            self.add_to_retry_queue(resource_item_id, priority=priority, status_code=e.status_code)
            return None  # request failed
        except ResourceNotFound as e:
            self.api_clients_info[api_client_dict['id']]['request_durations'][datetime.now()] = time() - start
            self.api_clients_info[api_client_dict['id']]['request_interval'] = api_client_dict['request_interval']
            logger.error('Resource not found {} at public: {}. {}'.format(self.resource[:-1],
                                                                          resource_item_id, e.message),
                         extra={'MESSAGE_ID': 'not_found_docs'})
            api_client_dict['client'].session.cookies.clear()
            logger.info('Clear client cookies')
            self.add_to_retry_queue(resource_item_id, priority=priority)
            self.api_clients_queue.put(api_client_dict)
            logger.debug('PUT API CLIENT: {}'.format(api_client_dict['id']), extra={'MESSAGE_ID': 'put_client'})
            return None  # not found
        except Exception as e:
            self.api_clients_info[api_client_dict['id']]['request_durations'][datetime.now()] = time() - start
            self.api_clients_info[api_client_dict['id']]['request_interval'] = api_client_dict['request_interval']
            self.api_clients_queue.put(api_client_dict)
            logger.debug('PUT API CLIENT: {}'.format(api_client_dict['id']), extra={'MESSAGE_ID': 'put_client'})
            logger.error(
                'Error while getting resource item {} {} from public {}: '.format(
                    self.resource[:-1], resource_item_id, e.message),
                extra={'MESSAGE_ID': 'exceptions'})
            self.add_to_retry_queue(resource_item_id, priority=priority)
        return None

    def _add_to_bulk(self, local_item, public_item, priority):
        public_item['doc_type'] = self.resource[:-1].title()
        public_item['_id'] = public_item['id']

        # Setup service keys
        if local_item:
            for k in local_item:
                if k.startswith('_'):
                    public_item[k] = local_item[k]

        bulk_doc = self.bulk.get(public_item['id'])

        if bulk_doc and bulk_doc['dateModified'] < public_item['dateModified']:
            logger.debug(
                'Replaced {} in bulk {} previous {}, current {}'.format(
                    self.resource[:-1], bulk_doc['id'], bulk_doc['dateModified'], public_item['dateModified']),
                extra={'MESSAGE_ID': 'skipped'})
            self.bulk[public_item['id']] = public_item
            if priority < self.priority_cache[public_item['id']]:
                self.priority_cache[public_item['id']] = priority
        elif bulk_doc and bulk_doc['dateModified'] >= public_item['dateModified']:
            logger.debug(
                'Ignored dublicate {} {} in bulk: previous {}, current {}'.format(
                    self.resource[:-1], public_item['id'], bulk_doc['dateModified'], public_item['dateModified']),
                extra={'MESSAGE_ID': 'skipped'})
        if not bulk_doc:
            self.bulk[public_item['id']] = public_item
            self.priority_cache[public_item['id']] = priority
            logger.debug('Put in bulk {} {} {}'.format(self.resource[:-1], public_item['id'],
                                                       public_item['dateModified']),
                         extra={'MESSAGE_ID': 'add_to_save_bulk'})
        return

    def log_timeshift(self, resource_item):
        ts = (datetime.now(TZ) - parse_date(resource_item['dateModified'])).total_seconds()
        logger.debug('{} {} timeshift is {} sec.'.format(self.resource[:-1], resource_item['id'], ts),
                     extra={'DOCUMENT_TIMESHIFT': ts})

    def _save_bulk_docs(self):
        if (len(self.bulk) > self.bulk_save_limit or (datetime.now() - self.start_time).total_seconds() >
                self.bulk_save_interval or self.exit):
            try:
                logger.debug('Try save bulk: {}'.format(len(self.bulk)), extra={'SAVE_BULK_LEN': len(self.bulk)})
                start = time()
                res = self.db.save_bulk(self.bulk)
                end = time() - start
                logger.debug('Bulk save duration: {} sec.'.format(end), extra={'SAVE_BULK_DURATION': end})
                for resource_item in self.bulk.values():
                    ts = (datetime.now(TZ) - parse_date(resource_item['dateModified'])).total_seconds()
                    logger.debug(
                        '{} {} timeshift is {} sec.'.format(self.resource[:-1], resource_item['id'], ts),
                        extra={'DOCUMENT_TIMESHIFT': ts}
                    )
                logger.info('Save bulk {} docs to db.'.format(len(self.bulk)))
            except Exception as e:
                logger.error('Error while saving bulk_docs in db: {}'.format(e.message),
                             extra={'MESSAGE_ID': 'exceptions'})
                for doc in self.bulk.values():
                    self.add_to_retry_queue(doc['id'], priority=self.priority_cache[doc['id']])
                self.start_time = datetime.now()
                self.priority_cache = {}
                self.bulk = {}
                return
            for success, doc_id, rev_or_exc in res:
                if success:
                    if not rev_or_exc.startswith('1-'):
                        logger.info('Update {} {}'.format(self.resource[:-1], doc_id),
                                    extra={'MESSAGE_ID': 'update_documents'})
                    else:
                        logger.info('Save {} {}'.format(self.resource[:-1], doc_id),
                                    extra={'MESSAGE_ID': 'save_documents'})
                    continue
                else:
                    if rev_or_exc.message != u'New doc with oldest dateModified.':
                        self.add_to_retry_queue(doc_id, priority=self.priority_cache[doc_id])
                        logger.error('Put to retry queue {} {} with reason: {}'.format(self.resource[:-1],
                                                                                       doc_id, rev_or_exc.message))
                    else:
                        logger.debug(
                            'Ignored {} {} with reason: {}'.format(self.resource[:-1], doc_id, rev_or_exc),
                            extra={'MESSAGE_ID': 'skiped'})
                        continue
            self.bulk = {}
            self.priority_cache = {}
            self.start_time = datetime.now()

    def _run(self):
        while not self.exit:
            # Try get api client from clients queue
            api_client_dict = self._get_api_client_dict()
            if api_client_dict is None:
                logger.debug('API clients queue is empty.')
                sleep(self.config['worker_sleep'])
                continue

            # Try get item from resource items queue
            priority, resource_item_id = self._get_resource_item_from_queue()
            if resource_item_id is None:
                self.api_clients_queue.put(api_client_dict)
                logger.debug('PUT API CLIENT: {}'.format(api_client_dict['id']), extra={'MESSAGE_ID': 'put_client'})
                logger.debug('Resource items queue is empty.')
                sleep(self.config['worker_sleep'])
                continue

            try:
                # Resource object from local db server
                local_resource_item = self.db.get_doc(resource_item_id)
            except Exception as e:
                self.api_clients_queue.put(api_client_dict)
                logger.debug('PUT API CLIENT: {}'.format(api_client_dict['id']), extra={'MESSAGE_ID': 'put_client'})
                self.add_to_retry_queue(resource_item_id, priority=priority)
                logger.error('Error while getting resource item from couchdb: {}'.format(repr(e)),
                             extra={'MESSAGE_ID': 'exceptions'})
                continue

            # Try get resource item from public server
            public_resource_item = self._get_resource_item_from_public(api_client_dict, priority, resource_item_id)
            if public_resource_item is None:
                continue

            # Add docs to bulk
            self._add_to_bulk(local_resource_item, public_resource_item, priority)

            # Save/Update docs in db
            self._save_bulk_docs()

    def shutdown(self):
        self.exit = True
        logger.info('Worker complete his job.')


@implementer(IWorker)
class AgreementWorker(Greenlet):

    def __init__(self, api_clients_queue=None, resource_items_queue=None, db=None, config_dict=None,
                 retry_resource_items_queue=None, api_clients_info=None):
        Greenlet.__init__(self)
        logger.info("Init CloseFrameworkAgreement UA Worker")
        self.cache_db = db
        self.main_config = config_dict
        self.config = config_dict['worker_config']
        self.input_resource = self.main_config['resource']
        self.resource = self.main_config['resource']
        self.input_resource_id = "{}_ID".format(self.main_config['resource'][:-1]).upper()
        self.api_clients_queue = api_clients_queue
        self.resource_items_queue = resource_items_queue
        self.retry_resource_items_queue = retry_resource_items_queue
        self.api_clients_info = api_clients_info
        self.start_time = datetime.now()
        self.exit = False

    def add_to_retry_queue(self, resource_item, priority=0, status_code=0):
        retries_count = priority - 1000 if priority >= 1000 else priority
        if retries_count > self.config['retries_count'] and status_code != 429:
            logger.critical(
                '{} {} reached limit retries count {} and droped from retry_queue.'.format(
                    self.resource[:-1].title(), resource_item['id'], self.config['retries_count']),
                extra=journal_context({'MESSAGE_ID': 'dropped_documents'}, {"TENDER_ID": resource_item['id']})
            )
            return
        timeout = 0
        if status_code != 429:
            timeout = self.config['retry_default_timeout'] * retries_count
            priority += 1
        sleep(timeout)
        self.retry_resource_items_queue.put((priority, resource_item))
        logger.info('Put to \'retry_queue\' {}: {}'.format(self.resource[:-1], resource_item['id']),
                    extra={'MESSAGE_ID': 'add_to_retry'})

    def _get_api_client_dict(self):
        if not self.api_clients_queue.empty():
            try:
                api_client_dict = self.api_clients_queue.get(timeout=self.config['queue_timeout'])
            except Empty:
                return None
            if self.api_clients_info[api_client_dict['id']]['drop_cookies']:
                try:
                    api_client_dict['client'].renew_cookies()
                    self.api_clients_info[api_client_dict['id']] = {
                        'drop_cookies': False,
                        'request_durations': {},
                        'request_interval': 0,
                        'avg_duration': 0
                    }
                    api_client_dict['request_interval'] = 0
                    api_client_dict['not_actual_count'] = 0
                    logger.debug('Drop lazy api_client {} cookies'.format(api_client_dict['id']))
                except (Exception, ConnectionError) as e:
                    self.api_clients_queue.put(api_client_dict)
                    logger.debug('PUT API CLIENT: {}'.format(api_client_dict['id']), extra={'MESSAGE_ID': 'put_client'})
                    logger.error('While renewing cookies catch exception: {}'.format(e.message))
                    return None
            logger.debug(
                'GET API CLIENT: {} {} with requests interval: {}'.format(
                    api_client_dict['id'],
                    api_client_dict['client'].session.headers['User-Agent'],
                    api_client_dict['request_interval']
                ),
                extra={'MESSAGE_ID': 'get_client', 'REQUESTS_TIMEOUT': api_client_dict['request_interval']}
            )
            sleep(api_client_dict['request_interval'])
            return api_client_dict
        else:
            return None

    def _get_resource_item_from_public(self, api_client_dict, priority, resource_item):
        try:
            logger.debug('Request interval {} sec. for client {}'.format(
                api_client_dict['request_interval'], api_client_dict['client'].session.headers['User-Agent']),
                extra={'REQUESTS_TIMEOUT': api_client_dict['request_interval']})
            start = time()
            public_resource_item = api_client_dict['client'].get_resource_item(resource_item['id']).get('data')
            self.api_clients_info[api_client_dict['id']]['request_durations'][datetime.now()] = time() - start
            self.api_clients_info[api_client_dict['id']]['request_interval'] = api_client_dict['request_interval']
            logger.debug('Recieved from API {}: {} {}'.format(self.resource[:-1], public_resource_item['id'],
                                                              public_resource_item['dateModified']))
            if api_client_dict['request_interval'] > 0:
                api_client_dict['request_interval'] -= self.config['client_dec_step_timeout']
            self.api_clients_queue.put(api_client_dict)
            logger.debug('PUT API CLIENT: {}'.format(api_client_dict['id']), extra={'MESSAGE_ID': 'put_client'})
            return public_resource_item
        except ResourceGone:
            self.api_clients_queue.put(api_client_dict)
            logger.debug('PUT API CLIENT: {}'.format(api_client_dict['id']), extra={'MESSAGE_ID': 'put_client'})
            logger.info('{} {} archived.'.format(self.resource[:-1].title(), resource_item['id']))
            return None  # Archived
        except InvalidResponse as e:
            self.api_clients_info[api_client_dict['id']]['request_durations'][datetime.now()] = time() - start
            self.api_clients_info[api_client_dict['id']]['request_interval'] = api_client_dict['request_interval']
            self.api_clients_queue.put(api_client_dict)
            logger.debug('PUT API CLIENT: {}'.format(api_client_dict['id']), extra={'MESSAGE_ID': 'put_client'})
            logger.error(
                'Error while getting {} {} from public with status code: {}'.format(self.resource[:-1],
                                                                                    resource_item['id'], e.status_code),
                extra={'MESSAGE_ID': 'exceptions'})
            self.add_to_retry_queue(resource_item, priority=priority)
            return None
        except RequestFailed as e:
            self.api_clients_info[api_client_dict['id']]['request_durations'][datetime.now()] = time() - start
            self.api_clients_info[api_client_dict['id']]['request_interval'] = api_client_dict['request_interval']
            if e.status_code == 429:
                if api_client_dict['request_interval'] > self.config['drop_threshold_client_cookies']:
                    api_client_dict['client'].session.cookies.clear()
                    api_client_dict['request_interval'] = 0
                else:
                    api_client_dict['request_interval'] += self.config['client_inc_step_timeout']
                self.api_clients_queue.put(api_client_dict, timeout=api_client_dict['request_interval'])
                logger.warning('PUT API CLIENT: {} after {} sec.'.format(api_client_dict['id'],
                                                                         api_client_dict['request_interval']),
                               extra={'MESSAGE_ID': 'put_client'})
            else:
                self.api_clients_queue.put(api_client_dict)
                logger.debug('PUT API CLIENT: {}'.format(api_client_dict['id']), extra={'MESSAGE_ID': 'put_client'})
            logger.error(
                'Request failed while getting {} {} from public with status code {}: '.format(
                    self.resource[:-1], resource_item['id'], e.status_code),
                extra={'MESSAGE_ID': 'exceptions'})
            self.add_to_retry_queue(resource_item, priority=priority, status_code=e.status_code)
            return None  # request failed
        except ResourceNotFound as e:
            self.api_clients_info[api_client_dict['id']]['request_durations'][datetime.now()] = time() - start
            self.api_clients_info[api_client_dict['id']]['request_interval'] = api_client_dict['request_interval']
            logger.error('Resource not found {} at public: {}. {}'.format(self.resource[:-1],
                                                                          resource_item['id'], e.message),
                         extra={'MESSAGE_ID': 'not_found_docs'})
            api_client_dict['client'].session.cookies.clear()
            logger.debug('Clear client cookies')
            self.api_clients_queue.put(api_client_dict)
            self.add_to_retry_queue(resource_item, priority=priority)
            logger.debug('PUT API CLIENT: {}'.format(api_client_dict['id']), extra={'MESSAGE_ID': 'put_client'})
            return None  # not found
        except Exception as e:
            self.api_clients_info[api_client_dict['id']]['request_durations'][datetime.now()] = time() - start
            self.api_clients_info[api_client_dict['id']]['request_interval'] = api_client_dict['request_interval']
            self.api_clients_queue.put(api_client_dict)
            logger.debug('PUT API CLIENT: {}'.format(api_client_dict['id']), extra={'MESSAGE_ID': 'put_client'})
            logger.error(
                'Error while getting resource item {} {} from public {}: '.format(self.resource[:-1],
                                                                                  resource_item['id'], e.message),
                extra={'MESSAGE_ID': 'exceptions'})
            self.add_to_retry_queue(resource_item, priority=priority)
        return None

    def _get_resource_item_from_queue(self):
        priority = resource_item = None
        if not self.resource_items_queue.empty():
            priority, resource_item = self.resource_items_queue.get(timeout=self.config['queue_timeout'])
            logger.debug('Get {} {} from main queue.'.format(self.input_resource[:-1], resource_item['id']))
        return priority, resource_item

    def log_timeshift(self, resource_item):
        ts = (datetime.now(TZ) - parse_date(resource_item['dateModified'])).total_seconds()
        logger.debug('{} {} timeshift is {} sec.'.format(self.input_resource[:-1], resource_item['id'], ts),
                     extra={'DOCUMENT_TIMESHIFT': ts})

    def _run(self):
        while not self.exit:
            # Try get api client from clients queue
            api_client_dict = self._get_api_client_dict()
            if api_client_dict is None:
                logger.critical('API clients queue is empty.')
                sleep(self.config['worker_sleep'])
                continue

            # Try get item from resource items queue
            priority, resource_item = self._get_resource_item_from_queue()
            if resource_item is None:
                self.api_clients_queue.put(api_client_dict)
                logger.debug('PUT API CLIENT: {}'.format(api_client_dict['id']), extra={'MESSAGE_ID': 'put_client'})
                logger.debug('Resource items queue is empty.')
                sleep(self.config['worker_sleep'])
                continue

            handler = handlers_registry.get(resource_item['procurementMethodType'], '')
            if not handler:
                handler = handlers_registry.get('common', '')
            if not handler:
                self.api_clients_queue.put(api_client_dict)
                logger.critical(
                    "Not found handler for procurementMethodType: {}, {} {}".format(
                        resource_item['procurementMethodType'], self.resource[:-1], resource_item['id']
                    ),
                    extra=journal_context({"MESSAGE_ID": "bridge_worker_exception"},
                                          {self.input_resource_id: resource_item['id']})
                )
                continue

            # Try get resource item from public server
            public_resource_item = self._get_resource_item_from_public(api_client_dict, priority, resource_item)
            if public_resource_item is None:
                continue

            try:
                handler.process_resource(public_resource_item)
            except RequestFailed as e:
                logger.error(
                    "Error while processing {} {}: {}".format(self.resource[:-1], resource_item['id'], e.message),
                    extra=journal_context({"MESSAGE_ID": "bridge_worker_exception"},
                                          {self.input_resource_id: resource_item['id']})
                )
                self.add_to_retry_queue(resource_item, priority)
            except Exception as e:
                logger.error(
                    "Error while processing {} {}: {}".format(self.resource[:-1], resource_item['id'], e.message),
                    extra=journal_context({"MESSAGE_ID": "bridge_worker_exception"},
                                          {self.input_resource_id: resource_item['id']})
                )
                self.add_to_retry_queue(resource_item, priority)

    def shutdown(self):
        self.exit = True
        logger.info('CloseFrameworkAgreement Worker complete his job.')
