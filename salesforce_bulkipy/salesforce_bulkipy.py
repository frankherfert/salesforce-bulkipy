from __future__ import absolute_import
from future.standard_library import install_aliases
from future.utils import iteritems
install_aliases()

import sys
import re
import time
import csv
from io import BytesIO
from tempfile import TemporaryFile
from collections import namedtuple
import xml.etree.ElementTree as ET

try:
    from StringIO import StringIO
except ImportError:
    from io import StringIO

try:
    # Python 2
    import urlparse
except ImportError:
    import urllib.parse as urlparse
from . import bulk_states

import simple_salesforce
import requests


UploadResult = namedtuple('UploadResult', 'id success created error')


class BulkApiError(Exception):
    def __init__(self, message, status_code=None):
        super(BulkApiError, self).__init__(message)
        self.status_code = status_code


class BulkJobAborted(BulkApiError):
    def __init__(self, job_id):
        self.job_id = job_id

        message = 'Job {0} aborted'.format(job_id)
        super(BulkJobAborted, self).__init__(message)


class BulkBatchFailed(BulkApiError):
    def __init__(self, job_id, batch_id, state_message):
        self.job_id = job_id
        self.batch_id = batch_id
        self.state_message = state_message

        message = 'Batch {0} of job {1} failed: {2}'.format(batch_id, job_id,
                                                            state_message)
        super(BulkBatchFailed, self).__init__(message)


class SalesforceBulkipy(object):
    def __init__(self, session_id=None, host=None, username=None, password=None, security_token=None, sandbox=False,
                 exception_class=BulkApiError, API_version="29.0"):
        if (not session_id or not host) and (not username or not password or not security_token):
            raise RuntimeError(
                "Must supply either sessionId,host or username,password,security_token")
        if username and password and security_token:
            session_id, host = SalesforceBulkipy.login_to_salesforce_using_username_password(username, password,
                                                                                             security_token, sandbox)

        if host[0:4] == 'http':
            self.endpoint = host
        else:
            self.endpoint = "https://" + host
        self.endpoint += "/services/async/%s" % API_version
        self.sessionId = session_id
        self.jobNS = 'http://www.force.com/2009/06/asyncapi/dataload'
        self.jobs = {}  # dict of job_id => job_id
        self.batches = {}  # dict of batch_id => job_id
        self.batch_statuses = {}
        self.exception_class = exception_class

    @staticmethod
    def login_to_salesforce_using_username_password(username, password, security_token, sandbox):
        sf = simple_salesforce.Salesforce(username=username, password=password, security_token=security_token,
                                          sandbox=sandbox)
        return sf.session_id, sf.sf_instance

    def headers(self, values={}):
        default = {"X-SFDC-Session": self.sessionId,
                   "Content-Type": "application/xml; charset=UTF-8"}
        for k, val in iteritems(values):
            default[k] = val
        return default

    # Register a new Bulk API job - returns the job id
    def create_query_job(self, object_name, **kwargs):
        return self.create_job(object_name, "query", **kwargs)

    def create_insert_job(self, object_name, **kwargs):
        return self.create_job(object_name, "insert", **kwargs)

    def create_upsert_job(self, object_name, external_id_name, **kwargs):
        return self.create_job(object_name, "upsert", external_id_name=external_id_name, **kwargs)

    def create_update_job(self, object_name, **kwargs):
        return self.create_job(object_name, "update", **kwargs)

    def create_delete_job(self, object_name, **kwargs):
        return self.create_job(object_name, "delete", **kwargs)

    def create_job(self, object_name=None, operation=None, contentType='CSV',
                   concurrency=None, external_id_name=None):
        assert (object_name is not None)
        assert (operation is not None)

        doc = self.create_job_doc(object_name=object_name,
                                  operation=operation,
                                  contentType=contentType,
                                  concurrency=concurrency,
                                  external_id_name=external_id_name)
        url = self.endpoint + '/job'

        resp = requests.post(url, headers=self.headers(), data=doc)
        self.check_status(resp, resp.content)

        tree = ET.fromstring(resp.content)
        job_id = tree.findtext("{%s}id" % self.jobNS)
        self.jobs[job_id] = job_id

        return job_id

    def check_status(self, resp, content):
        if resp.status_code >= 400:
            msg = "Bulk API HTTP Error result: {0}".format(content)
            self.raise_error(msg, resp.status_code)

    def close_job(self, job_id):
        doc = self.create_close_job_doc()
        url = self.endpoint + "/job/%s" % job_id

        resp = requests.post(url, headers=self.headers(), data=doc)
        self.check_status(resp, resp.content)

    def abort_job(self, job_id):
        """Abort a given bulk job"""
        doc = self.create_abort_job_doc()
        url = self.endpoint + "/job/%s" % job_id

        resp = requests.post(url, headers=self.headers(), data=doc)
        self.check_status(resp, resp.content)

    def create_job_doc(self, object_name=None, operation=None,
                       contentType='CSV', concurrency=None, external_id_name=None):
        root = ET.Element("jobInfo")
        root.set("xmlns", self.jobNS)
        op = ET.SubElement(root, "operation")
        op.text = operation
        obj = ET.SubElement(root, "object")
        obj.text = object_name
        if external_id_name:
            ext = ET.SubElement(root, 'externalIdFieldName')
            ext.text = external_id_name

        if concurrency:
            con = ET.SubElement(root, "concurrencyMode")
            con.text = concurrency
        ct = ET.SubElement(root, "contentType")
        ct.text = contentType

        return self._xml_element_to_str(root)

    def create_close_job_doc(self):
        root = ET.Element("jobInfo")
        root.set("xmlns", self.jobNS)
        state = ET.SubElement(root, "state")
        state.text = "Closed"

        return self._xml_element_to_str(root)

    def create_abort_job_doc(self):
        """Create XML doc for aborting a job"""
        root = ET.Element("jobInfo")
        root.set("xmlns", self.jobNS)
        state = ET.SubElement(root, "state")
        state.text = "Aborted"

        return self._xml_element_to_str(root)

    # Add a BulkQuery to the job - returns the batch id
    def query(self, job_id, soql):
        if job_id is None:
            job_id = self.create_job(
                re.search(re.compile("from (\w+)", re.I), soql).group(1),
                "query")

        uri = self.endpoint + "/job/%s/batch" % job_id
        headers = self.headers({"Content-Type": "text/csv"})

        resp = requests.post(uri, headers=headers, data=soql)
        self.check_status(resp, resp.content)

        tree = ET.fromstring(resp.content)
        batch_id = tree.findtext("{%s}id" % self.jobNS)

        self.batches[batch_id] = job_id

        return batch_id

    def split_csv(self, csv, batch_size):
        csv_io = StringIO(csv)
        batches = []
        batch = ''
        headers = ''

        for i, line in enumerate(csv_io):
            if not i:
                headers = line
                batch = headers
                continue
            if not i % batch_size:
                batches.append(batch)
                batch = headers

            batch += line

        batches.append(batch)

        return batches

    # Add a BulkUpload to the job - returns the batch id
    def bulk_csv_upload(self, job_id, csv, batch_size=2500):
        # Split a large CSV into manageable batches
        batches = self.split_csv(csv, batch_size)
        batch_ids = []

        uri = self.endpoint + "/job/%s/batch" % job_id
        headers = self.headers({"Content-Type": "text/csv"})
        for batch in batches:
            resp = requests.post(uri, data=batch, headers=headers)
            content = resp.content

            if resp.status_code >= 400:
                self.raise_error(content, resp.status)

            tree = ET.fromstring(content)
            batch_id = tree.findtext("{%s}id" % self.jobNS)

            self.batches[batch_id] = job_id
            batch_ids.append(batch_id)

        return batch_ids

    def raise_error(self, message, status_code=None):
        if status_code:
            message = "[{0}] {1}".format(status_code, message)

        if self.exception_class == BulkApiError:
            raise self.exception_class(message, status_code=status_code)
        else:
            raise self.exception_class(message)

    def post_bulk_batch(self, job_id, csv_generator):
        uri = self.endpoint + "/job/%s/batch" % job_id
        headers = self.headers({"Content-Type": "text/csv"})
        resp = requests.post(uri, data=csv_generator, headers=headers)
        content = resp.content

        if resp.status_code >= 400:
            self.raise_error(content, resp.status_code)

        tree = ET.fromstring(content)
        batch_id = tree.findtext("{%s}id" % self.jobNS)
        return batch_id

    # Add a BulkDelete to the job - returns the batch id
    def bulk_delete(self, job_id, object_type, where, batch_size=2500):
        query_job_id = self.create_query_job(object_type)
        soql = "Select Id from %s where %s Limit 10000" % (object_type, where)
        query_batch_id = self.query(query_job_id, soql)
        self.wait_for_batch(query_job_id, query_batch_id, timeout=120)

        results = self.get_all_results_for_batch(batch_id=query_batch_id, job_id=query_job_id)
        if job_id is None:
            job_id = self.create_delete_job(object_type)

        batch_ids = []

        uri = self.endpoint + "/job/%s/batch" % job_id
        headers = self.headers({"Content-Type": "text/csv"})
        for batch in results:
            batch_data = '\n'.join(list(batch))
            resp = requests.post(uri, data=batch_data, headers=headers)
            content = resp.content

            if resp.status_code >= 400:
                self.raise_error(content, resp.status_code)

            tree = ET.fromstring(content)
            batch_id = tree.findtext("{%s}id" % self.jobNS)

            self.batches[batch_id] = job_id
            batch_ids.append(batch_id)

        self.close_job(query_job_id)
        return batch_ids

    def lookup_job_id(self, batch_id):
        try:
            return self.batches[batch_id]
        except KeyError:
            raise Exception(
                "Batch id '%s' is uknown, can't retrieve job_id" % batch_id)

    def job_status(self, job_id=None):
        job_id = job_id or self.lookup_job_id(job_id)
        uri = urlparse.urljoin(self.endpoint + "/",
                               'job/{0}'.format(job_id))
        response = requests.get(uri, headers=self.headers())
        if response.status_code != 200:
            self.raise_error(response.content, response.status_code)

        tree = ET.fromstring(response.content)
        result = {}
        for child in tree:
            result[re.sub("{.*?}", "", child.tag)] = child.text
        return result

    def job_state(self, job_id):
        status = self.job_status(job_id)
        if 'state' in status:
            return status['state']
        else:
            return None

    def batch_status(self, job_id=None, batch_id=None, reload=False):
        if not reload and batch_id in self.batch_statuses:
            return self.batch_statuses[batch_id]

        job_id = job_id or self.lookup_job_id(batch_id)
        uri = self.endpoint + \
              "/job/%s/batch/%s" % (job_id, batch_id)

        resp = requests.get(uri, headers=self.headers())
        self.check_status(resp, resp.content)

        tree = ET.fromstring(resp.content)
        result = {}
        for child in tree:
            result[re.sub("{.*?}", "", child.tag)] = child.text

        self.batch_statuses[batch_id] = result
        return result

    def batch_state(self, job_id, batch_id, reload=False):
        status = self.batch_status(job_id, batch_id, reload=reload)
        if 'state' in status:
            return status['state']
        else:
            return None

    def is_batch_done(self, job_id, batch_id):
        batch_state = self.batch_state(job_id, batch_id, reload=True)
        if batch_state in bulk_states.ERROR_STATES:
            status = self.batch_status(job_id, batch_id)
            raise BulkBatchFailed(job_id, batch_id, status['stateMessage'])
        return batch_state == bulk_states.COMPLETED

    # Wait for the given batch to complete, waiting at most timeout seconds
    # (defaults to 10 minutes).
    def wait_for_batch(self, job_id, batch_id, timeout=60 * 10,
                       sleep_interval=10):
        waited = 0
        while not self.is_batch_done(job_id, batch_id) and waited < timeout:
            time.sleep(sleep_interval)
            waited += sleep_interval

    def get_batch_result_ids(self, batch_id, job_id=None):
        job_id = job_id or self.lookup_job_id(batch_id)
        if not self.is_batch_done(job_id, batch_id):
            return False

        uri = urlparse.urljoin(
            self.endpoint + "/",
            "job/{0}/batch/{1}/result".format(
                job_id, batch_id),
        )
        resp = requests.get(uri, headers=self.headers())
        if resp.status_code != 200:
            return False

        tree = ET.fromstring(resp.content)
        find_func = getattr(tree, 'iterfind', tree.findall)
        return [str(r.text) for r in
                find_func("{{{0}}}result".format(self.jobNS))]

    def get_all_results_for_batch(self, batch_id, job_id=None, parse_csv=False, logger=None):
        """
        Gets result ids and generates each result set from the batch and returns it
        as an generator fetching the next result set when needed
        Args:
            batch_id: id of batch
            job_id: id of job, if not provided, it will be looked up
            parse_csv: if true, results will be dictionaries instead of lines
        """
        result_ids = self.get_batch_result_ids(batch_id, job_id=job_id)
        if not result_ids:
            if logger:
                logger.error('Batch is not complete, may have timed out. '
                             'batch_id: %s, job_id: %s', batch_id, job_id)
            raise RuntimeError('Batch is not complete')
        for result_id in result_ids:
            yield self.get_batch_results(
                batch_id,
                result_id,
                job_id=job_id,
                parse_csv=parse_csv)

    def get_batch_results(self, batch_id, result_id, job_id=None,
                          parse_csv=False, logger=None):
        job_id = job_id or self.lookup_job_id(batch_id)
        logger = logger or (lambda message: None)

        uri = urlparse.urljoin(
            self.endpoint + "/",
            "job/{0}/batch/{1}/result/{2}".format(
                job_id, batch_id, result_id),
        )
        logger('Downloading bulk result file id=#{0}'.format(result_id))
        resp = requests.get(uri, headers=self.headers(), stream=True)

        if parse_csv:
            iterator = csv.reader(
                self._unicode_list_gen(resp.iter_lines()), delimiter=',', quotechar='"')
        else:
            iterator = self._unicode_list_gen(resp.iter_lines())

        BATCH_SIZE = 5000
        for i, line in enumerate(iterator):
            if i % BATCH_SIZE == 0:
                logger('Loading bulk result #{0}'.format(i))
            if parse_csv:
                yield list(self._unicode_list_gen(line))
            else:
                yield self._unicode_converter(line)

    def get_batch_result_iter(self, job_id, batch_id, parse_csv=False,
                              logger=None):
        """
        Return a line interator over the contents of a batch result document. If
        csv=True then parses the first line as the csv header and the iterator
        returns dicts.
        """
        status = self.batch_status(job_id, batch_id)
        if status['state'] != 'Completed':
            return None
        elif logger:
            if 'numberRecordsProcessed' in status:
                logger("Bulk batch %d processed %s records" %
                       (batch_id, status['numberRecordsProcessed']))
            if 'numberRecordsFailed' in status:
                failed = int(status['numberRecordsFailed'])
                if failed > 0:
                    logger("Bulk batch %d had %d failed records" %
                           (batch_id, failed))

        uri = self.endpoint + \
              "/job/%s/batch/%s/result" % (job_id, batch_id)
        r = requests.get(uri, headers=self.headers(), stream=True)

        result_id = r.text.split("<result>")[1].split("</result>")[0]

        uri = self.endpoint + \
              "/job/%s/batch/%s/result/%s" % (job_id, batch_id, result_id)
        r = requests.get(uri, headers=self.headers(), stream=True)

        if parse_csv:
            reader = csv.DictReader(
                self._unicode_list_gen(r.iter_lines(chunk_size=2048)),
                delimiter=',',
                quotechar='"')
            return self._unicode_list_dicts_gen(reader)
        else:
            return self._unicode_list_gen(r.iter_lines(chunk_size=2048))

    def get_upload_results(self, job_id, batch_id,
                           callback=(lambda *args, **kwargs: None),
                           batch_size=0, logger=None):
        job_id = job_id or self.lookup_job_id(batch_id)

        if not self.is_batch_done(job_id, batch_id):
            return False

        uri = self.endpoint + \
              "/job/%s/batch/%s/result" % (job_id, batch_id)
        resp = requests.get(uri, headers=self.headers())

        tf = TemporaryFile()
        tf.write(resp.content)

        total_remaining = self.count_file_lines(tf)
        if logger:
            logger("Total records: %d" % total_remaining)
        tf.seek(0)

        records = []
        line_number = 0
        col_names = []
        tf_text = tf.read()
        reader = csv.reader(
            self._unicode_list_gen(tf_text.splitlines()), delimiter=",", quotechar='"')
        for row in reader:
            line_number += 1
            records.append(UploadResult(*row))
            if len(records) == 1:
                col_names = records[0]
            if batch_size > 0 and len(records) >= (batch_size + 1):
                callback(records, total_remaining, line_number)
                total_remaining -= (len(records) - 1)
                records = [col_names]
        callback(records, total_remaining, line_number)

        tf.close()

        return True

    def parse_csv(self, tf, callback, batch_size, total_remaining):
        records = []
        line_number = 0
        col_names = []
        reader = csv.reader(tf, delimiter=",", quotechar='"')
        for row in reader:
            line_number += 1
            records.append(row)
            if len(records) == 1:
                col_names = records[0]
            if batch_size > 0 and len(records) >= (batch_size + 1):
                callback(records, total_remaining, line_number)
                total_remaining -= (len(records) - 1)
                records = [col_names]
        return records, total_remaining

    def count_file_lines(self, tf):
        tf.seek(0)
        buffer = bytearray(2048)
        lines = 0

        quotes = 0
        while tf.readinto(buffer) > 0:
            quoteChar = ord('"')
            newline = ord('\n')
            for c in buffer:
                if c == quoteChar:
                    quotes += 1
                elif c == newline:
                    if (quotes % 2) == 0:
                        lines += 1
                        quotes = 0

        return lines

    @staticmethod
    def _xml_element_to_str(root):
        """ Converts a xml.etree.ElementTree.Element to string

        Args:
            root (xml.etree.ElementTree.Element): the tree element

        Returns:
            The string representation of the given element
        """

        buf = BytesIO()
        tree = ET.ElementTree(root)
        tree.write(buf, encoding="UTF-8", xml_declaration=True)
        return buf.getvalue().decode('utf-8')

    @staticmethod
    def _unicode_converter(input_data):
        """ Converts a string/byte array to a unicode string in Py 2 and Py 3
        Args:
            input_data (str|unicode|bytes): the array to be converted

        Returns:
            a unicode string
        """

        if sys.version_info[0] == 2:
            # py2
            if isinstance(input_data, unicode):
                return input_data
            else:
                return str(input_data)
        else:
            # py3
            try:
                return input_data.decode('utf-8')
            except AttributeError:
                return input_data

    @classmethod
    def _unicode_list_gen(cls, input_list):
        """ Converts a list of str or bytes to an iterable of converted unicode str

        Args:
            input_list: the list of str or bytes

        Returns:
            an iterable of converted unicode str
        """

        return (cls._unicode_converter(x) for x in input_list)

    @classmethod
    def _unicode_list_dicts_gen(cls, input_list):
        """ Converts a list of dicts with keys/values as str/bytes to unicode str

        Args:
            input_list: a list of dicts

        Returns:
            a list of dicts having the keys/values converted to unicode strings
        """

        return ({
            cls._unicode_converter(key): cls._unicode_converter(val)
            for key, val in item_dict.items()} for item_dict in input_list)
