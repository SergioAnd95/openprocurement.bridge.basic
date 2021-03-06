# -*- coding: utf-8 -*-
import datetime
import logging
import unittest
import uuid
from copy import deepcopy

import iso8601
from gevent import sleep, idle
from gevent.queue import Empty, Queue, PriorityQueue
from mock import MagicMock, call, patch
from munch import munchify
from openprocurement_client.exceptions import ResourceNotFound as RNF
from openprocurement_client.exceptions import (InvalidResponse, RequestFailed,
                                               ResourceGone)

from openprocurement.bridge.basic.workers import TZ, BasicResourceItemWorker, AgreementWorker, logger
from openprocurement.bridge.basic.tests.base import TEST_CONFIG

logger.setLevel(logging.DEBUG)


class TestResourceItemWorker(unittest.TestCase):

    config = deepcopy(TEST_CONFIG['main'])

    worker_config = config['worker_config']
    worker_config['client_inc_step_timeout'] = 0.1
    worker_config['client_dec_step_timeout'] = 0.02
    worker_config['drop_threshold_client_cookies'] = 1.5
    worker_config['worker_sleep'] = 0.1
    worker_config['retry_default_timeout'] = 0.5
    worker_config['retries_count'] = 2
    worker_config['queue_timeout'] = 0.3
    worker_config['bulk_save_limit'] = 1
    worker_config['bulk_save_interval'] = 0.1

    def tearDown(self):
        self.worker_config['client_inc_step_timeout'] = 0.1
        self.worker_config['client_dec_step_timeout'] = 0.02
        self.worker_config['drop_threshold_client_cookies'] = 1.5
        self.worker_config['worker_sleep'] = 0.03
        self.worker_config['retry_default_timeout'] = 0.05
        self.worker_config['retries_count'] = 2
        self.worker_config['queue_timeout'] = 0.03

    def test_init(self):
        worker = BasicResourceItemWorker(
            'api_clients_queue', 'resource_items_queue', 'db',
            {'worker_config': {'bulk_save_limit': 1, 'bulk_save_interval': 1}, 'resource': 'tender'},
            'retry_resource_items_queue')
        self.assertEqual(worker.api_clients_queue, 'api_clients_queue')
        self.assertEqual(worker.resource_items_queue, 'resource_items_queue')
        self.assertEqual(worker.db, 'db')
        self.assertEqual(worker.config, {'bulk_save_limit': 1, 'bulk_save_interval': 1})
        self.assertEqual(worker.retry_resource_items_queue, 'retry_resource_items_queue')
        self.assertEqual(worker.exit, False)
        self.assertEqual(worker.update_doc, False)

    @patch('openprocurement.bridge.basic.workers.logger')
    def test_add_to_retry_queue(self, mocked_logger):
        retry_items_queue = PriorityQueue()
        worker = BasicResourceItemWorker(config_dict=self.config, retry_resource_items_queue=retry_items_queue)
        resource_item_id = uuid.uuid4().hex
        priority = 1000
        self.assertEqual(retry_items_queue.qsize(), 0)

        # Add to retry_resource_items_queue
        worker.add_to_retry_queue(resource_item_id, priority=priority)
        # sleep(worker.config['retry_default_timeout'] * 0)
        self.assertEqual(retry_items_queue.qsize(), 1)
        priority, retry_resource_item_id = retry_items_queue.get()
        self.assertEqual(priority, 1001)
        self.assertEqual(retry_resource_item_id, resource_item_id)

        # Add to retry_resource_items_queue with status_code '429'
        worker.add_to_retry_queue(resource_item_id, priority, status_code=429)
        self.assertEqual(retry_items_queue.qsize(), 1)
        priority, retry_resource_item_id = retry_items_queue.get()
        self.assertEqual(priority, 1001)
        self.assertEqual(retry_resource_item_id, resource_item_id)

        priority = 1002
        worker.add_to_retry_queue(resource_item_id, priority=priority)
        sleep(worker.config['retry_default_timeout'] * 2)
        self.assertEqual(retry_items_queue.qsize(), 1)
        priority, retry_resource_item_id = retry_items_queue.get()
        self.assertEqual(priority, 1003)
        self.assertEqual(retry_resource_item_id, resource_item_id)

        worker.add_to_retry_queue(resource_item_id, priority=priority)
        self.assertEqual(retry_items_queue.qsize(), 0)
        mocked_logger.critical.assert_called_once_with(
            'Tender {} reached limit retries count {} and droped from retry_queue.'.format(
                resource_item_id, worker.config['retries_count']),
            extra={'MESSAGE_ID': 'dropped_documents'}
        )
        del worker

    @patch('openprocurement.bridge.basic.workers.logger')
    @patch('openprocurement.bridge.basic.workers.datetime')
    def test_log_timeshift(self, mocked_datetime, mocked_logger):
        mocked_datetime.now.return_value = datetime.datetime(2017, 1, 1, 0, 1, tzinfo=TZ)
        date_modified = datetime.datetime(2017, 1, 1, 0, 0, tzinfo=TZ).isoformat()
        worker = BasicResourceItemWorker(config_dict=self.config)
        resource_item = {'id': uuid.uuid4().hex, 'dateModified': date_modified}
        worker.log_timeshift(resource_item)
        mocked_logger.debug.assert_called_once_with(
            'tender {} timeshift is 60.0 sec.'.format(resource_item['id']),
            extra={'DOCUMENT_TIMESHIFT': 60.0})

    def test__get_api_client_dict(self):
        api_clients_queue = Queue()
        client = MagicMock()
        client_dict = {
            'id': uuid.uuid4().hex,
            'client': client,
            'request_interval': 0
        }
        client_dict2 = {
            'id': uuid.uuid4().hex,
            'client': client,
            'request_interval': 0
        }
        api_clients_queue.put(client_dict)
        api_clients_queue.put(client_dict2)
        api_clients_info = {
            client_dict['id']: {
                'drop_cookies': False,
                'not_actual_count': 5,
                'request_interval': 3
            },
            client_dict2['id']: {
                'drop_cookies': True,
                'not_actual_count': 3,
                'request_interval': 2
            }
        }

        # Success test
        worker = BasicResourceItemWorker(api_clients_queue=api_clients_queue, config_dict=self.config,
                                         api_clients_info=api_clients_info)
        self.assertEqual(worker.api_clients_queue.qsize(), 2)
        api_client = worker._get_api_client_dict()
        self.assertEqual(api_client, client_dict)

        # Get lazy client
        api_client = worker._get_api_client_dict()
        self.assertEqual(api_client['not_actual_count'], 0)
        self.assertEqual(api_client['request_interval'], 0)

        # Empty queue test
        api_client = worker._get_api_client_dict()
        self.assertEqual(api_client, None)

        # Exception when try renew cookies
        client.renew_cookies.side_effect = Exception('Can\'t renew cookies')
        worker.api_clients_queue.put(client_dict2)
        api_clients_info[client_dict2['id']]['drop_cookies'] = True
        api_client = worker._get_api_client_dict()
        self.assertIs(api_client, None)
        self.assertEqual(worker.api_clients_queue.qsize(), 1)
        self.assertEqual(worker.api_clients_queue.get(), client_dict2)

        # Get api_client with raise Empty exception
        api_clients_queue.put(client_dict2)
        api_clients_queue = MagicMock()
        api_clients_queue.get.side_effect = Empty
        api_client = worker._get_api_client_dict()
        self.assertEqual(api_client, None)
        del worker

    def test__get_resource_item_from_queue(self):
        items_queue = PriorityQueue()
        item = (1, uuid.uuid4().hex)
        items_queue.put(item)

        # Success test
        worker = BasicResourceItemWorker(resource_items_queue=items_queue, config_dict=self.config)
        self.assertEqual(worker.resource_items_queue.qsize(), 1)
        priority, resource_item = worker._get_resource_item_from_queue()
        self.assertEqual((priority, resource_item), item)
        self.assertEqual(worker.resource_items_queue.qsize(), 0)

        # Empty queue test
        priority, resource_item = worker._get_resource_item_from_queue()
        self.assertEqual(resource_item, None)
        self.assertEqual(priority, None)
        del worker

    @patch('openprocurement.bridge.basic.databridge.APIClient')
    def test__get_resource_item_from_public(self, mock_api_client):
        resource_item_id = uuid.uuid4().hex
        priority = 1

        api_clients_queue = Queue()
        client_dict = {
            'id': uuid.uuid4().hex,
            'request_interval': 0.02,
            'client': mock_api_client
        }
        api_clients_queue.put(client_dict)
        api_clients_info = {client_dict['id']: {'drop_cookies': False, 'request_durations': {}}}
        retry_queue = PriorityQueue()
        return_dict = {
            'data': {
                'id': resource_item_id,
                'dateModified': datetime.datetime.utcnow().isoformat()
            }
        }
        mock_api_client.get_resource_item.return_value = return_dict
        worker = BasicResourceItemWorker(api_clients_queue=api_clients_queue,
                                         config_dict=self.config,
                                         retry_resource_items_queue=retry_queue,
                                         api_clients_info=api_clients_info)

        # Success test
        self.assertEqual(worker.api_clients_queue.qsize(), 1)
        api_client = worker._get_api_client_dict()
        self.assertEqual(api_client['request_interval'], 0.02)
        self.assertEqual(worker.api_clients_queue.qsize(), 0)
        public_item = worker._get_resource_item_from_public(api_client, priority, resource_item_id)
        self.assertEqual(worker.retry_resource_items_queue.qsize(), 0)
        self.assertEqual(public_item, return_dict['data'])

        # InvalidResponse
        mock_api_client.get_resource_item.side_effect = InvalidResponse('invalid response')
        api_client = worker._get_api_client_dict()
        self.assertEqual(worker.api_clients_queue.qsize(), 0)
        public_item = worker._get_resource_item_from_public(api_client, priority, resource_item_id)
        self.assertEqual(public_item, None)
        sleep(worker.config['retry_default_timeout'] * 1)
        self.assertEqual(worker.retry_resource_items_queue.qsize(), 1)
        self.assertEqual(worker.api_clients_queue.qsize(), 1)

        # RequestFailed status_code=429
        mock_api_client.get_resource_item.side_effect = RequestFailed(munchify({'status_code': 429}))
        api_client = worker._get_api_client_dict()
        self.assertEqual(worker.api_clients_queue.qsize(), 0)
        self.assertEqual(api_client['request_interval'], 0)
        public_item = worker._get_resource_item_from_public(api_client, priority, resource_item_id)
        self.assertEqual(public_item, None)
        sleep(worker.config['retry_default_timeout'] * 2)
        self.assertEqual(worker.retry_resource_items_queue.qsize(), 2)
        self.assertEqual(worker.api_clients_queue.qsize(), 1)
        api_client = worker._get_api_client_dict()
        self.assertEqual(worker.api_clients_queue.qsize(), 0)
        self.assertEqual(api_client['request_interval'], worker.config['client_inc_step_timeout'])

        # RequestFailed status_code=429 with drop cookies
        api_client['request_interval'] = 2
        public_item = worker._get_resource_item_from_public(api_client, priority, resource_item_id)
        sleep(api_client['request_interval'])
        self.assertEqual(worker.api_clients_queue.qsize(), 1)
        self.assertEqual(public_item, None)
        self.assertEqual(api_client['request_interval'], 0)
        sleep(worker.config['retry_default_timeout'] * 2)
        self.assertEqual(worker.retry_resource_items_queue.qsize(), 3)

        # RequestFailed with status_code not equal 429
        mock_api_client.get_resource_item.side_effect = RequestFailed(munchify({'status_code': 404}))
        api_client = worker._get_api_client_dict()
        self.assertEqual(worker.api_clients_queue.qsize(), 0)
        public_item = worker._get_resource_item_from_public(api_client, priority, resource_item_id)
        self.assertEqual(public_item, None)
        self.assertEqual(worker.api_clients_queue.qsize(), 1)
        self.assertEqual(api_client['request_interval'], 0)
        sleep(worker.config['retry_default_timeout'] * 2)
        self.assertEqual(worker.retry_resource_items_queue.qsize(), 4)

        # ResourceNotFound
        mock_api_client.get_resource_item.side_effect = RNF(munchify({'status_code': 404}))
        api_client = worker._get_api_client_dict()
        self.assertEqual(worker.api_clients_queue.qsize(), 0)
        public_item = worker._get_resource_item_from_public(api_client, priority, resource_item_id)
        self.assertEqual(public_item, None)
        self.assertEqual(worker.api_clients_queue.qsize(), 1)
        self.assertEqual(api_client['request_interval'], 0)
        sleep(worker.config['retry_default_timeout'] * 2)
        self.assertEqual(worker.retry_resource_items_queue.qsize(), 5)

        # ResourceGone
        mock_api_client.get_resource_item.side_effect = ResourceGone(munchify({'status_code': 410}))
        api_client = worker._get_api_client_dict()
        self.assertEqual(worker.api_clients_queue.qsize(), 0)
        public_item = worker._get_resource_item_from_public(api_client, priority, resource_item_id)
        self.assertEqual(public_item, None)
        self.assertEqual(worker.api_clients_queue.qsize(), 1)
        self.assertEqual(api_client['request_interval'], 0)
        sleep(worker.config['retry_default_timeout'] * 2)
        self.assertEqual(worker.retry_resource_items_queue.qsize(), 5)

        # Exception
        api_client = worker._get_api_client_dict()
        mock_api_client.get_resource_item.side_effect = Exception('text except')
        public_item = worker._get_resource_item_from_public(api_client, priority, resource_item_id)
        self.assertEqual(public_item, None)
        self.assertEqual(api_client['request_interval'], 0)
        sleep(worker.config['retry_default_timeout'] * 2)
        self.assertEqual(worker.retry_resource_items_queue.qsize(), 6)

        del worker

    def test__add_to_bulk(self):
        retry_queue = PriorityQueue()
        old_date_modified = datetime.datetime.utcnow().isoformat()
        resource_item_id = uuid.uuid4().hex
        priority = 1

        local_resource_item = {
            'doc_type': 'Tender',
            '_rev': '1-' + uuid.uuid4().hex,
            'id': resource_item_id,
            'dateModified': old_date_modified
        }
        new_date_modified = datetime.datetime.utcnow().isoformat()
        public_resource_item = {
            'id': resource_item_id,
            'dateModified': new_date_modified
        }
        worker = BasicResourceItemWorker(config_dict=self.config, retry_resource_items_queue=retry_queue)
        worker.db = MagicMock()

        # Successfull adding to bulk
        start_length = len(worker.bulk)
        worker._add_to_bulk(local_resource_item, public_resource_item, priority)
        end_length = len(worker.bulk)
        self.assertGreater(end_length, start_length)

        # Update exist doc in bulk
        start_length = len(worker.bulk)
        new_public_resource_item = deepcopy(public_resource_item)
        new_public_resource_item['dateModified'] = datetime.datetime.utcnow().isoformat()
        worker._add_to_bulk(local_resource_item, new_public_resource_item, priority)
        end_length = len(worker.bulk)
        self.assertEqual(start_length, end_length)

        # Ignored dublicate in bulk
        start_length = end_length
        worker._add_to_bulk(local_resource_item, {
            'doc_type': 'Tender',
            'id': local_resource_item['id'],
            '_id': local_resource_item['id'],
            'dateModified': old_date_modified
        }, priority)
        end_length = len(worker.bulk)
        self.assertEqual(start_length, end_length)
        del worker

    def test__save_bulk_docs(self):
        self.worker_config['bulk_save_limit'] = 3
        retry_queue = PriorityQueue()
        worker = BasicResourceItemWorker(config_dict=self.config, retry_resource_items_queue=retry_queue)
        doc_id_1 = uuid.uuid4().hex
        doc_id_2 = uuid.uuid4().hex
        doc_id_3 = uuid.uuid4().hex
        doc_id_4 = uuid.uuid4().hex
        worker.priority_cache[doc_id_1] = 1
        worker.priority_cache[doc_id_2] = 1
        worker.priority_cache[doc_id_3] = 1
        worker.priority_cache[doc_id_4] = 1
        date_modified = datetime.datetime.utcnow().isoformat()
        worker.bulk = {
            doc_id_1: {'id': doc_id_1, 'dateModified': date_modified},
            doc_id_2: {'id': doc_id_2, 'dateModified': date_modified},
            doc_id_3: {'id': doc_id_3, 'dateModified': date_modified},
            doc_id_4: {'id': doc_id_4, 'dateModified': date_modified}
        }
        update_return_value = [
            (True, doc_id_1, '1-' + uuid.uuid4().hex),
            (True, doc_id_2, '2-' + uuid.uuid4().hex),
            (False, doc_id_3, Exception(u'New doc with oldest dateModified.')),
            (False, doc_id_4, Exception(u'Document update conflict.'))
        ]
        worker.db = MagicMock()
        worker.db.save_bulk.return_value = update_return_value

        # Test success response from couchdb
        worker._save_bulk_docs()
        sleep(0.1)
        self.assertEqual(len(worker.bulk), 0)
        self.assertEqual(worker.retry_resource_items_queue.qsize(), 1)

        # Test failed response from couchdb
        worker.db.save_bulk.side_effect = Exception('Some exceptions')
        worker.bulk = {
            doc_id_1: {'id': doc_id_1, 'dateModified': date_modified},
            doc_id_2: {'id': doc_id_2, 'dateModified': date_modified},
            doc_id_3: {'id': doc_id_3, 'dateModified': date_modified},
            doc_id_4: {'id': doc_id_4, 'dateModified': date_modified}
        }
        worker.priority_cache[doc_id_1] = 1
        worker.priority_cache[doc_id_2] = 1
        worker.priority_cache[doc_id_3] = 1
        worker.priority_cache[doc_id_4] = 1
        worker._save_bulk_docs()
        sleep(0.2)
        self.assertEqual(worker.retry_resource_items_queue.qsize(), 5)
        self.assertEqual(len(worker.bulk), 0)

    def test_shutdown(self):
        worker = BasicResourceItemWorker(
            'api_clients_queue', 'resource_items_queue', 'db',
            {'worker_config': {'bulk_save_limit': 1, 'bulk_save_interval': 1}, 'resource': 'tenders'},
            'retry_resource_items_queue')
        self.assertEqual(worker.exit, False)
        worker.shutdown()
        self.assertEqual(worker.exit, True)

    @patch('openprocurement.bridge.basic.workers.BasicResourceItemWorker._save_bulk_docs')
    @patch('openprocurement.bridge.basic.workers.BasicResourceItemWorker._get_resource_item_from_public')
    @patch('openprocurement.bridge.basic.workers.logger')
    def test__run(self, mocked_logger, mock_get_from_public, mocked_save_bulk):
        self.queue = Queue()
        self.retry_queue = Queue()
        self.api_clients_queue = Queue()
        queue_item = (1, uuid.uuid4().hex)
        doc = {
            'id': queue_item[1],
            '_rev': '1-{}'.format(uuid.uuid4().hex),
            'dateModified': datetime.datetime.utcnow().isoformat(),
            'doc_type': 'Tender'
        }
        client = MagicMock()
        api_client_dict = {
            'id': uuid.uuid4().hex,
            'client': client,
            'request_interval': 0
        }
        client.session.headers = {'User-Agent': 'Test-Agent'}
        self.api_clients_info = {
            api_client_dict['id']: {
                'drop_cookies': False, 'request_durations': []
            }
        }
        self.db = MagicMock()
        worker = BasicResourceItemWorker(
            api_clients_queue=self.api_clients_queue,
            resource_items_queue=self.queue,
            retry_resource_items_queue=self.retry_queue,
            db=self.db, api_clients_info=self.api_clients_info,
            config_dict=self.config
        )
        worker.exit = MagicMock()
        worker.exit.__nonzero__.side_effect = [False, True]

        # Try get api client from clients queue
        self.assertEqual(self.queue.qsize(), 0)
        worker._run()
        self.assertEqual(self.queue.qsize(), 0)
        mocked_logger.debug.assert_called_once_with('API clients queue is empty.')

        # Try get item from resource items queue
        self.api_clients_queue.put(api_client_dict)
        worker.exit.__nonzero__.side_effect = [False, True]
        worker._run()
        self.assertEqual(
            mocked_logger.debug.call_args_list[1:],
            [
                call(
                    'GET API CLIENT: {} {} with requests interval: {}'.format(
                        api_client_dict['id'],
                        api_client_dict['client'].session.headers['User-Agent'],
                        api_client_dict['request_interval']
                    ),
                    extra={'REQUESTS_TIMEOUT': 0, 'MESSAGE_ID': 'get_client'}
                ),
                call('PUT API CLIENT: {}'.format(api_client_dict['id']), extra={'MESSAGE_ID': 'put_client'}),
                call('Resource items queue is empty.')
            ]
        )

        # Try get resource item from local storage
        self.queue.put(queue_item)
        mock_get_from_public.return_value = doc
        worker.exit.__nonzero__.side_effect = [False, True]
        worker._run()
        self.assertEqual(
            mocked_logger.debug.call_args_list[4:],
            [
                call(
                    'GET API CLIENT: {} {} with requests interval: {}'.format(
                        api_client_dict['id'],
                        api_client_dict['client'].session.headers['User-Agent'],
                        api_client_dict['request_interval']
                    ),
                    extra={'REQUESTS_TIMEOUT': 0, 'MESSAGE_ID': 'get_client'}
                ),
                call('Get tender {} from main queue.'.format(doc['id'])),
                call('Put in bulk tender {} {}'.format(doc['id'], doc['dateModified']),
                     extra={'MESSAGE_ID': 'add_to_save_bulk'})
            ]
        )

        # Try get local_resource_item with Exception
        self.api_clients_queue.put(api_client_dict)
        self.queue.put(queue_item)
        mock_get_from_public.return_value = doc
        self.db.get_doc.side_effect = [Exception('Database Error')]
        worker.exit.__nonzero__.side_effect = [False, True]
        worker._run()
        self.assertEqual(
            mocked_logger.debug.call_args_list[7:],
            [
                call(
                    'GET API CLIENT: {} {} with requests interval: {}'.format(
                        api_client_dict['id'],
                        api_client_dict['client'].session.headers['User-Agent'],
                        api_client_dict['request_interval']
                    ),
                    extra={'REQUESTS_TIMEOUT': 0, 'MESSAGE_ID': 'get_client'}
                ),
                call('Get tender {} from main queue.'.format(doc['id'])),
                call('PUT API CLIENT: {}'.format(api_client_dict['id']), extra={'MESSAGE_ID': 'put_client'})
            ]
        )
        mocked_logger.error.assert_called_once_with(
            "Error while getting resource item from couchdb: Exception('Database Error',)",
            extra={'MESSAGE_ID': 'exceptions'}
        )

        self.api_clients_queue.put(api_client_dict)
        self.queue.put(queue_item)
        mock_get_from_public.return_value = None
        self.db.get_doc.side_effect = [doc]
        worker.exit.__nonzero__.side_effect = [False, True]
        worker._run()
        self.assertEqual(
            mocked_logger.debug.call_args_list[10:],
            [
                call(
                    'GET API CLIENT: {} {} with requests interval: {}'.format(
                        api_client_dict['id'],
                        api_client_dict['client'].session.headers['User-Agent'],
                        api_client_dict['request_interval']
                    ),
                    extra={'REQUESTS_TIMEOUT': 0, 'MESSAGE_ID': 'get_client'}
                ),
                call('Get tender {} from main queue.'.format(doc['id'])),
            ]
        )
        self.assertEqual(mocked_save_bulk.call_count, 1)


class TestResourceAgreementWorker(unittest.TestCase):
    worker_config = {
        'worker_config': {
            'worker_type': 'basic_couchdb',
            'client_inc_step_timeout': 0.1,
            'client_dec_step_timeout': 0.02,
            'drop_threshold_client_cookies': 1.5,
            'worker_sleep': 5,
            'retry_default_timeout': 0.5,
            'retries_count': 2,
            'queue_timeout': 3,
            'bulk_save_limit': 100,
            'bulk_save_interval': 3
        },
        'storage_config': {
            # required for databridge
            "storage_type": "couchdb",  # possible values ['couchdb', 'elasticsearch']
            # arguments for storage configuration
            "host": "localhost",
            "port": 5984,
            "user": "",
            "password": "",
            "db_name": "basic_bridge_db",
            "bulk_query_interval": 3,
            "bulk_query_limit": 100,
        },
        'filter_type': 'basic_couchdb',
        'retrievers_params': {
            'down_requests_sleep': 5,
            'up_requests_sleep': 1,
            'up_wait_sleep': 30,
            'queue_size': 1001
        },
        'extra_params': {
            "mode": "_all_",
            "limit": 1000
        },
        'bridge_mode': 'basic',
        'resources_api_server': 'http://localhost:1234',
        'resources_api_version': "0",
        'resources_api_token': '',
        'public_resources_api_server': 'http://localhost:1234',
        'resource': 'tenders',
        'workers_inc_threshold': 75,
        'workers_dec_threshold': 35,
        'workers_min': 1,
        'workers_max': 3,
        'filter_workers_count': 1,
        'retry_workers_min': 1,
        'retry_workers_max': 2,
        'retry_resource_items_queue_size': -1,
        'watch_interval': 10,
        'user_agent': 'bridge.basic',
        'resource_items_queue_size': 10000,
        'input_queue_size': 10000,
        'resource_items_limit': 1000,
        'queues_controller_timeout': 60,
        'perfomance_window': 300
    }

    def tearDown(self):
        self.worker_config['resource'] = 'tenders'
        self.worker_config['client_inc_step_timeout'] = 0.1
        self.worker_config['client_dec_step_timeout'] = 0.02
        self.worker_config['drop_threshold_client_cookies'] = 1.5
        self.worker_config['worker_sleep'] = 0.03
        self.worker_config['retry_default_timeout'] = 0.01
        self.worker_config['retries_count'] = 2
        self.worker_config['queue_timeout'] = 0.03

    def test_init(self):
        worker = AgreementWorker('api_clients_queue', 'resource_items_queue', 'db',
                                 {'worker_config': {'bulk_save_limit': 1, 'bulk_save_interval': 1},
                                  'resource': 'tenders'}, 'retry_resource_items_queue')
        self.assertEqual(worker.api_clients_queue, 'api_clients_queue')
        self.assertEqual(worker.resource_items_queue, 'resource_items_queue')
        self.assertEqual(worker.cache_db, 'db')
        self.assertEqual(worker.config, {'bulk_save_limit': 1, 'bulk_save_interval': 1})
        self.assertEqual(worker.retry_resource_items_queue, 'retry_resource_items_queue')
        self.assertEqual(worker.input_resource_id, 'TENDER_ID')
        self.assertEqual((worker.api_clients_info, worker.exit), (None, False))

    @patch('openprocurement.bridge.basic.workers.logger')
    def test_add_to_retry_queue(self, mocked_logger):
        retry_items_queue = PriorityQueue()
        worker = AgreementWorker(config_dict=self.worker_config, retry_resource_items_queue=retry_items_queue)
        resource_item = {'id': uuid.uuid4().hex}
        priority = 1000
        self.assertEqual(retry_items_queue.qsize(), 0)

        # Add to retry_resource_items_queue
        worker.add_to_retry_queue(resource_item, priority=priority)

        self.assertEqual(retry_items_queue.qsize(), 1)
        priority, retry_resource_item = retry_items_queue.get()
        self.assertEqual((priority, retry_resource_item), (1001, resource_item))

        resource_item = {'id': 0}
        # Add to retry_resource_items_queue with status_code '429'
        worker.add_to_retry_queue(resource_item, priority, status_code=429)
        self.assertEqual(retry_items_queue.qsize(), 1)
        priority, retry_resource_item = retry_items_queue.get()
        self.assertEqual((priority, retry_resource_item), (1001, resource_item))

        priority = 1002
        worker.add_to_retry_queue(resource_item, priority=priority)
        sleep(worker.config['retry_default_timeout'] * 2)
        self.assertEqual(retry_items_queue.qsize(), 1)
        priority, retry_resource_item = retry_items_queue.get()
        self.assertEqual((priority, retry_resource_item), (1003, resource_item))

        worker.add_to_retry_queue(resource_item, priority=priority)
        self.assertEqual(retry_items_queue.qsize(), 0)
        mocked_logger.critical.assert_called_once_with(
            'Tender {} reached limit retries count {} and droped from '
            'retry_queue.'.format(resource_item['id'], worker.config['retries_count']),
            extra={'MESSAGE_ID': 'dropped_documents', 'JOURNAL_TENDER_ID': resource_item['id']}
        )
        del worker

    def test__get_api_client_dict(self):
        api_clients_queue = Queue()
        client = MagicMock()
        client_dict = {
            'id': uuid.uuid4().hex,
            'client': client,
            'request_interval': 0
        }
        client_dict2 = {
            'id': uuid.uuid4().hex,
            'client': client,
            'request_interval': 0
        }
        api_clients_queue.put(client_dict)
        api_clients_queue.put(client_dict2)
        api_clients_info = {
            client_dict['id']: {
                'drop_cookies': False,
                'not_actual_count': 5,
                'request_interval': 3
            },
            client_dict2['id']: {
                'drop_cookies': True,
                'not_actual_count': 3,
                'request_interval': 2
            }
        }

        # Success test
        worker = AgreementWorker(api_clients_queue=api_clients_queue, config_dict=self.worker_config,
                                 api_clients_info=api_clients_info)
        self.assertEqual(worker.api_clients_queue.qsize(), 2)
        api_client = worker._get_api_client_dict()
        self.assertEqual(api_client, client_dict)

        # Get lazy client
        api_client = worker._get_api_client_dict()
        self.assertEqual(api_client['not_actual_count'], 0)
        self.assertEqual(api_client['request_interval'], 0)

        # Empty queue test
        api_client = worker._get_api_client_dict()
        self.assertEqual(api_client, None)

        # Exception when try renew cookies
        client.renew_cookies.side_effect = Exception('Can\'t renew cookies')
        worker.api_clients_queue.put(client_dict2)
        api_clients_info[client_dict2['id']]['drop_cookies'] = True
        api_client = worker._get_api_client_dict()
        self.assertIs(api_client, None)
        self.assertEqual(worker.api_clients_queue.qsize(), 1)
        self.assertEqual(worker.api_clients_queue.get(), client_dict2)

        # Get api_client with raise Empty exception
        api_clients_queue = MagicMock()
        api_clients_queue.empty.return_value = False
        api_clients_queue.get = MagicMock(side_effect=Empty)
        worker.api_clients_queue = api_clients_queue
        api_client = worker._get_api_client_dict()

        self.assertEqual(api_client, None)
        del worker

    def test__get_resource_item_from_queue(self):
        items_queue = PriorityQueue()
        item = (1, {'id': uuid.uuid4().hex})
        items_queue.put(item)

        # Success test
        worker = AgreementWorker(resource_items_queue=items_queue, config_dict=self.worker_config)
        self.assertEqual(worker.resource_items_queue.qsize(), 1)
        priority, resource_item = worker._get_resource_item_from_queue()
        self.assertEqual((priority, resource_item), item)
        self.assertEqual(worker.resource_items_queue.qsize(), 0)

        # Empty queue test
        priority, resource_item = worker._get_resource_item_from_queue()
        self.assertEqual(resource_item, None)
        self.assertEqual(priority, None)
        del worker

    @patch('openprocurement_client.client.TendersClient')
    def test__get_resource_item_from_public(self, mock_api_client):
        resource_item = {'id': uuid.uuid4().hex}
        resource_item_id = uuid.uuid4().hex
        priority = 1

        api_clients_queue = Queue()
        client_dict = {
            'id': uuid.uuid4().hex,
            'request_interval': 0.02,
            'client': mock_api_client
        }
        api_clients_queue.put(client_dict)
        api_clients_info = {client_dict['id']: {'drop_cookies': False, 'request_durations': {}}}
        retry_queue = PriorityQueue()
        return_dict = {
            'data': {
                'id': resource_item_id,
                'dateModified': datetime.datetime.utcnow().isoformat()
            }
        }
        mock_api_client.get_resource_item.return_value = return_dict
        worker = AgreementWorker(api_clients_queue=api_clients_queue, config_dict=self.worker_config,
                                 retry_resource_items_queue=retry_queue, api_clients_info=api_clients_info)

        # Success test
        self.assertEqual(worker.api_clients_queue.qsize(), 1)
        api_client = worker._get_api_client_dict()
        self.assertEqual(api_client['request_interval'], 0.02)
        self.assertEqual(worker.api_clients_queue.qsize(), 0)
        public_item = worker._get_resource_item_from_public(
            api_client, priority, resource_item
        )
        self.assertEqual(worker.retry_resource_items_queue.qsize(), 0)
        self.assertEqual(public_item, return_dict['data'])

        # InvalidResponse
        mock_api_client.get_resource_item.side_effect = InvalidResponse('invalid response')
        api_client = worker._get_api_client_dict()
        self.assertEqual(worker.api_clients_queue.qsize(), 0)
        public_item = worker._get_resource_item_from_public(api_client, priority, resource_item)
        self.assertEqual(public_item, None)
        sleep(worker.config['retry_default_timeout'] * 1)
        self.assertEqual(worker.retry_resource_items_queue.qsize(), 1)
        self.assertEqual(worker.api_clients_queue.qsize(), 1)

        # RequestFailed status_code=429
        mock_api_client.get_resource_item.side_effect = RequestFailed(munchify({'status_code': 429}))
        api_client = worker._get_api_client_dict()
        self.assertEqual(worker.api_clients_queue.qsize(), 0)
        self.assertEqual(api_client['request_interval'], 0)
        public_item = worker._get_resource_item_from_public(api_client, priority, resource_item)
        self.assertEqual(public_item, None)
        sleep(worker.config['retry_default_timeout'] * 2)
        self.assertEqual(worker.retry_resource_items_queue.qsize(), 2)
        self.assertEqual(worker.api_clients_queue.qsize(), 1)
        api_client = worker._get_api_client_dict()
        self.assertEqual(worker.api_clients_queue.qsize(), 0)
        self.assertEqual(api_client['request_interval'],
                         worker.config['client_inc_step_timeout'])

        # RequestFailed status_code=429 with drop cookies
        api_client['request_interval'] = 2
        public_item = worker._get_resource_item_from_public(api_client, priority, resource_item)
        sleep(api_client['request_interval'])
        self.assertEqual(worker.api_clients_queue.qsize(), 1)
        self.assertEqual(public_item, None)
        self.assertEqual(api_client['request_interval'], 0)
        sleep(worker.config['retry_default_timeout'] * 2)
        self.assertEqual(worker.retry_resource_items_queue.qsize(), 3)

        # RequestFailed with status_code not equal 429
        mock_api_client.get_resource_item.side_effect = RequestFailed(
            munchify({'status_code': 404}))
        api_client = worker._get_api_client_dict()
        self.assertEqual(worker.api_clients_queue.qsize(), 0)
        public_item = worker._get_resource_item_from_public(api_client, priority, resource_item)
        self.assertEqual(public_item, None)
        self.assertEqual(worker.api_clients_queue.qsize(), 1)
        self.assertEqual(api_client['request_interval'], 0)
        sleep(worker.config['retry_default_timeout'] * 2)
        self.assertEqual(worker.retry_resource_items_queue.qsize(), 4)

        # ResourceNotFound
        mock_api_client.get_resource_item.side_effect = RNF(munchify({'status_code': 404}))
        api_client = worker._get_api_client_dict()
        self.assertEqual(worker.api_clients_queue.qsize(), 0)
        public_item = worker._get_resource_item_from_public(api_client, priority, resource_item)
        self.assertEqual(public_item, None)
        self.assertEqual(worker.api_clients_queue.qsize(), 1)
        self.assertEqual(api_client['request_interval'], 0)
        sleep(worker.config['retry_default_timeout'] * 2)
        self.assertEqual(worker.retry_resource_items_queue.qsize(), 5)

        # ResourceGone
        mock_api_client.get_resource_item.side_effect = ResourceGone(munchify({'status_code': 410}))
        api_client = worker._get_api_client_dict()
        self.assertEqual(worker.api_clients_queue.qsize(), 0)
        public_item = worker._get_resource_item_from_public(api_client, priority, resource_item)
        self.assertEqual(public_item, None)
        self.assertEqual(worker.api_clients_queue.qsize(), 1)
        self.assertEqual(api_client['request_interval'], 0)
        sleep(worker.config['retry_default_timeout'] * 2)
        self.assertEqual(worker.retry_resource_items_queue.qsize(), 5)

        # Exception
        api_client = worker._get_api_client_dict()
        mock_api_client.get_resource_item.side_effect = Exception('text except')
        public_item = worker._get_resource_item_from_public(api_client, priority, resource_item)
        self.assertEqual(public_item, None)
        self.assertEqual(api_client['request_interval'], 0)
        sleep(worker.config['retry_default_timeout'] * 2)
        self.assertEqual(worker.retry_resource_items_queue.qsize(), 6)

        del worker

    def test_shutdown(self):
        worker = AgreementWorker('api_clients_queue', 'resource_items_queue', 'db',
                                 {'worker_config': {'bulk_save_limit': 1, 'bulk_save_interval': 1},
                                  'resource': 'tenders'}, 'retry_resource_items_queue')
        self.assertEqual(worker.exit, False)
        worker.shutdown()
        self.assertEqual(worker.exit, True)

    def up_worker(self):
        worker_thread = AgreementWorker.spawn(
            resource_items_queue=self.queue,
            retry_resource_items_queue=self.retry_queue,
            api_clients_info=self.api_clients_info,
            api_clients_queue=self.api_clients_queue,
            config_dict=self.worker_config, db=self.db)
        idle()
        worker_thread.shutdown()
        sleep(3)

    @patch('openprocurement.bridge.basic.workers.handlers_registry')
    @patch('openprocurement.bridge.basic.workers.AgreementWorker._get_resource_item_from_public')
    @patch('openprocurement.bridge.basic.workers.logger')
    def test__run(self, mocked_logger, mock_get_from_public, mock_registry):
        self.queue = Queue()
        self.retry_queue = Queue()
        self.api_clients_queue = Queue()
        queue_item = (1, {'id': uuid.uuid4().hex, 'procurementMethodType': 'closeFrameworkAgreementUA'})
        doc = {
            'id': queue_item[1],
            '_rev': '1-{}'.format(uuid.uuid4().hex),
            'dateModified': datetime.datetime.utcnow().isoformat(),
            'doc_type': 'Tender'
        }
        client = MagicMock()
        api_client_dict = {
            'id': uuid.uuid4().hex,
            'client': client,
            'request_interval': 0
        }
        client.session.headers = {'User-Agent': 'Test-Agent'}
        self.api_clients_info = {
            api_client_dict['id']: {'drop_cookies': False, 'request_durations': []}
        }
        self.db = MagicMock()
        worker = AgreementWorker(
            api_clients_queue=self.api_clients_queue,
            resource_items_queue=self.queue,
            retry_resource_items_queue=self.retry_queue,
            db=self.db, api_clients_info=self.api_clients_info,
            config_dict=self.worker_config
        )
        worker.exit = MagicMock()
        worker.exit.__nonzero__.side_effect = [False, True]

        # Try get api client from clients queue
        self.assertEqual(self.queue.qsize(), 0)
        worker._run()
        self.assertEqual(self.queue.qsize(), 0)
        mocked_logger.critical.assert_called_once_with(
            'API clients queue is empty.')

        # Try get item from resource items queue with no handler
        self.api_clients_queue.put(api_client_dict)
        worker.exit.__nonzero__.side_effect = [False, True]
        mock_registry.get.return_value = ''
        self.queue.put(queue_item)
        mock_get_from_public.return_value = doc
        worker._run()
        self.assertEqual(
            mocked_logger.critical.call_args_list,
            [
                call('API clients queue is empty.'),
                call(
                    'Not found handler for procurementMethodType: {}, {} {}'.format(
                        doc['id']['procurementMethodType'],
                        self.worker_config['resource'][:-1],
                        doc['id']['id']
                    ),
                    extra={'JOURNAL_TENDER_ID': doc['id']['id'], 'MESSAGE_ID': 'bridge_worker_exception'}
                )
            ]
        )

        # Try get item from resource items queue
        self.api_clients_queue.put(api_client_dict)
        worker.exit.__nonzero__.side_effect = [False, True]
        handler_mock = MagicMock()
        handler_mock.process_resource.return_value = None
        mock_registry.return_value = {'closeFrameworkAgreementUA': handler_mock}
        worker._run()
        self.assertEqual(
            mocked_logger.debug.call_args_list[2:],
            [
                call(
                    'GET API CLIENT: {} {} with requests interval: {}'.format(
                        api_client_dict['id'],
                        api_client_dict['client'].session.headers['User-Agent'],
                        api_client_dict['request_interval']
                    ),
                    extra={'REQUESTS_TIMEOUT': 0, 'MESSAGE_ID': 'get_client'}
                ),
                call('PUT API CLIENT: {}'.format(api_client_dict['id']),
                     extra={'MESSAGE_ID': 'put_client'}),
                call('Resource items queue is empty.')
            ]
        )

        # Try get resource item from local storage
        self.queue.put(queue_item)
        mock_get_from_public.return_value = doc
        worker.exit.__nonzero__.side_effect = [False, True]
        worker._run()
        self.assertEqual(
            mocked_logger.debug.call_args_list[5:],
            [
                call(
                    'GET API CLIENT: {} {} with requests interval: {}'.format(
                        api_client_dict['id'],
                        api_client_dict['client'].session.headers['User-Agent'],
                        api_client_dict['request_interval']
                    ),
                    extra={'REQUESTS_TIMEOUT': 0, 'MESSAGE_ID': 'get_client'}
                ),
                call('Get tender {} from main queue.'.format(doc['id']['id']))
            ]
        )

        # Try get local_resource_item with Exception
        self.api_clients_queue.put(api_client_dict)
        self.queue.put(queue_item)
        mock_get_from_public.return_value = doc
        self.db.get.side_effect = [Exception('Database Error')]
        worker.exit.__nonzero__.side_effect = [False, True]
        worker._run()
        self.assertEqual(
            mocked_logger.debug.call_args_list[7:],
            [
                call(
                    'GET API CLIENT: {} {} with requests interval: {}'.format(
                        api_client_dict['id'],
                        api_client_dict['client'].session.headers['User-Agent'],
                        api_client_dict['request_interval']
                    ),
                    extra={'REQUESTS_TIMEOUT': 0, 'MESSAGE_ID': 'get_client'}
                ),
                call('Get tender {} from main queue.'.format(doc['id']['id']))
            ]
        )

        # Try process resource with Exception
        self.api_clients_queue.put(api_client_dict)
        self.queue.put(queue_item)
        mock_get_from_public.return_value = doc
        worker.exit.__nonzero__.side_effect = [False, True]

        mock_handler = MagicMock()
        mock_handler.process_resource.side_effect = (RequestFailed(),)
        mock_registry.get.return_value = mock_handler

        worker._run()
        self.assertEqual(
            mocked_logger.error.call_args_list,
            [
                call(
                    'Error while processing {} {}: {}'.format(
                        self.worker_config['resource'][:-1],
                        doc['id']['id'],
                        'Not described error yet.'
                    ),
                    extra={'JOURNAL_TENDER_ID': doc['id']['id'], 'MESSAGE_ID': 'bridge_worker_exception'}
                )
            ]
        )
        check_queue_item = (queue_item[0] + 1, queue_item[1])  # priority is increased
        self.assertEquals(self.retry_queue.get(), check_queue_item)

        # Try process resource with Exception
        self.api_clients_queue.put(api_client_dict)
        self.queue.put(queue_item)
        mock_get_from_public.return_value = doc
        worker.exit.__nonzero__.side_effect = [False, True]

        mock_handler = MagicMock()
        mock_handler.process_resource.side_effect = (Exception(),)
        mock_registry.get.return_value = mock_handler
        worker._run()

        self.assertEqual(
            mocked_logger.error.call_args_list[1:],
            [
                call(
                    'Error while processing {} {}: {}'.format(
                        self.worker_config['resource'][:-1],
                        doc['id']['id'],
                        ''
                    ),
                    extra={'JOURNAL_TENDER_ID': doc['id']['id'], 'MESSAGE_ID': 'bridge_worker_exception'}
                )
            ]
        )
        check_queue_item = (queue_item[0] + 1, queue_item[1])  # priority is increased
        self.assertEquals(self.retry_queue.get(), check_queue_item)

        #  No resource item
        self.api_clients_queue.put(api_client_dict)
        self.queue.put(queue_item)
        mock_get_from_public.return_value = None
        worker.exit.__nonzero__.side_effect = [False, True]

        mock_handler = MagicMock()
        mock_handler.process_resource.side_effect = (Exception(),)
        mock_registry.get.return_value = mock_handler
        worker._run()

        self.assertEquals(self.queue.empty(), True)
        self.assertEquals(self.retry_queue.empty(), True)

    @patch('openprocurement.bridge.basic.workers.datetime')
    @patch('openprocurement.bridge.basic.workers.logger')
    def test_log_timeshift(self, mocked_logger, mocked_datetime):
        worker = AgreementWorker('api_clients_queue', 'resource_items_queue', 'db',
                                 {'worker_config': {'bulk_save_limit': 1, 'bulk_save_interval': 1},
                                  'resource': 'tenders'}, 'retry_resource_items_queue')

        time_var = datetime.datetime.now(iso8601.UTC)

        mocked_datetime.now.return_value = time_var
        resource_item = {'id': '0' * 32,
                         'dateModified': time_var.isoformat()}
        worker.log_timeshift(resource_item)

        self.assertEqual(
            mocked_logger.debug.call_args_list,
            [
                call(
                    '{} {} timeshift is {} sec.'.format(
                        self.worker_config['resource'][:-1],
                        resource_item['id'],
                        0.0
                    ),
                    extra={'DOCUMENT_TIMESHIFT': 0.0}
                )
            ]
        )


def suite():
    suite = unittest.TestSuite()
    suite.addTest(unittest.makeSuite(TestResourceItemWorker))
    suite.addTest(unittest.makeSuite(TestResourceAgreementWorker))
    return suite


if __name__ == '__main__':
    unittest.main(defaultTest='suite')
