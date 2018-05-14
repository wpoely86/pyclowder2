"""Connectors

The system has two connectors defined by default. The connectors are used to
start the extractors. The connector will look for messages and call the
check_message and process_message of the extractor. The two connectors are
RabbitMQConnector and HPCConnector. Both of these will call the check_message
first, and based on the result of this will ignore the message, download the
file and call process_message or bypass the download and just call the
process_message.

RabbitMQConnector

The RabbitMQ connector connects to a RabbitMQ instance, creates a queue and
binds itself to that queue. Any message in the queue will be fetched and
passed to the check_message and process_message. This connector takesthree
parameters:

* rabbitmq_uri [REQUIRED] : the uri of the RabbitMQ server
* rabbitmq_exchange [OPTIONAL] : the exchange to which to bind the queue
* rabbitmq_key [OPTIONAL] : the key that binds the queue to the exchange

HPCConnector

The HPC connector will run extractions based on the pickle files that are
passed in to the constructor as an argument. Once all pickle files are
processed the extractor will stop. The pickle file is assumed to have one
additional argument, the logfile that is being monitored to send feedback
back to clowder. This connector takes a single argument (which can be list):

* picklefile [REQUIRED] : a single file, or list of files that are the
                          pickled messsages to be processed.
"""

import json
import logging
import os
import pickle
import subprocess
import time
import tempfile
import threading
import errno

import pika
import requests

import pyclowder.datasets
import pyclowder.files
import pyclowder.utils


class Connector(object):
    """ Class that will listen for messages.

     Once a message is received this will start the extraction process. It is assumed
    that there is only one Connector per thread.
    """

    registered_clowder = list()

    def __init__(self, extractor_info, check_message=None, process_message=None, ssl_verify=True, mounted_paths=None):
        self.extractor_info = extractor_info
        self.check_message = check_message
        self.process_message = process_message
        self.ssl_verify = ssl_verify
        if mounted_paths is None:
            self.mounted_paths = {}
        else:
            self.mounted_paths = mounted_paths

    def listen(self):
        """Listen for incoming messages.

         This function will not return until all messages are processed, or the process is
         interrupted.
         """
        pass

    def alive(self):
        """Return whether connection is still alive or not."""
        return True

    def _build_resource(self, body, host, secret_key):
        """Examine message body and create resource object based on message type.

        Example FILE message -- *.file.#
        {   "filename":         name of the triggering file without path,
            "id":               UUID of the triggering file
            "intermediateId":   UUID of the triggering file (deprecated)
            "datasetId":        UUID of dataset that holds the file
            "host":             URL of Clowder host; can include things like 'localhost' from browser address bar
            "secretKey":        API secret key for Clowder host
            "fileSize":         file size in bytes
            "flags":            any additional flags
        }

        Example DATASET message -- *.dataset.file.#
        {   "id":               UUID of the triggering file
            "intermediateId":   UUID of the triggering file (deprecated)
            "datasetId":        UUID of dataset that holds the file
            "host":             URL of Clowder host; can include things like 'localhost' from browser address bar
            "secretKey":        API secret key for Clowder host
            "fileSize":         file size in bytes
            "flags":            any additional flags
        }

        Example METADATA message -- *.metadata.#
        {   "resourceType":     what type of object metadata was added to; 'file' or 'dataset'
            "resourceId":       UUID of the triggering resource (file or dataset)
            "metadata":         actual metadata that was added or removed
            "id":               UUID of the triggering file (blank for 'dataset' type)
            "intermediateId":   (deprecated)
            "datasetId":        UUID of the triggering dataset (blank for 'file' type)
            "host":             URL of Clowder host; can include things like 'localhost' from browser address bar
            "secretKey":        API secret key for Clowder host
            "fileSize":         file size in bytes
            "flags":            any additional flags
        }
        """

        logger = logging.getLogger(__name__)

        # See docstring for information about these fields
        fileid = body.get('id', '')
        intermediatefileid = body.get('intermediateId', '')
        datasetid = body.get('datasetId', '')
        filename = body.get('filename', '')

        # determine resource type; defaults to file
        resource_type = "file"
        message_type = body['routing_key']
        if message_type.find(".dataset.") > -1:
            resource_type = "dataset"
        elif message_type.find(".file.") > -1:
            resource_type = "file"
        elif message_type.find("metadata.added") > -1:
            resource_type = "metadata"
        elif message_type == "extractors."+self.extractor_info['name']:
            # This was a manually submitted extraction
            if datasetid == fileid:
                resource_type = "dataset"
            else:
                resource_type = "file"
        elif message_type.endswith(self.extractor_info['name']):
            # This was migrated from another queue (e.g. error queue) so use extractor default
            for key, value in self.extractor_info['process'].iteritems():
                if key == "dataset":
                    resource_type = "dataset"
                else:
                    resource_type = "file"

        # determine what to download (if needed) and add relevant data to resource
        if resource_type == "dataset":
            try:
                datasetinfo = pyclowder.datasets.get_info(self, host, secret_key, datasetid)
                filelist = pyclowder.datasets.get_file_list(self, host, secret_key, datasetid)
                triggering_file = None
                for f in filelist:
                    if f['id'] == fileid:
                        triggering_file = f['filename']
                        break

                return {
                    "type": "dataset",
                    "id": datasetid,
                    "name": datasetinfo["name"],
                    "files": filelist,
                    "triggering_file": triggering_file,
                    "parent": {},
                    "dataset_info": datasetinfo
                }
            except:
                msg = "[%s] : Error downloading dataset preprocess information." % datasetid
                logger.exception(msg)
                # Can't create full resource object but can provide essential details for status_update
                resource = {
                    "type": "dataset",
                    "id": datasetid
                }
                self.status_update(pyclowder.utils.StatusMessage.error, resource, msg)
                self.message_error(resource)
                return None

        elif resource_type == "file":
            ext = os.path.splitext(filename)[1]
            return {
                "type": "file",
                "id": fileid,
                "intermediate_id": intermediatefileid,
                "name": filename,
                "file_ext": ext,
                "parent": {"type": "dataset",
                           "id": datasetid}
            }

        elif resource_type == "metadata":
            return {
                "type": "metadata",
                "id": body['resourceId'],
                "parent": {"type": body['resourceType'],
                           "id": body['resourceId']},
                "metadata": body['metadata']
            }

    def _check_for_local_file(self, host, secret_key, file_metadata):
        """ Try to get pointer to locally accessible copy of file for extractor."""

        # first check if file is accessible locally
        if 'filepath' in file_metadata:
            file_path = file_metadata['filepath']

            # first simply check if file is present locally
            if os.path.isfile(file_path):
                return file_path

            # otherwise check any mounted paths...
            if len(self.mounted_paths) > 0:
                for source_path in self.mounted_paths:
                    if file_path.startswith(source_path):
                        return file_path.replace(source_path, self.mounted_paths[source_path])

        return None

    def _download_file_metadata(self, host, secret_key, fileid, filepath):
        """Download metadata for a file into a temporary _metadata.json file.

        Returns:
            (tmp directory created, tmp file created)
        """
        file_md = pyclowder.files.download_metadata(self, host, secret_key, fileid)
        md_name = os.path.basename(filepath)+"_metadata.json"

        md_dir = tempfile.mkdtemp(suffix=fileid)
        (fd, md_file) = tempfile.mkstemp(suffix=md_name, dir=md_dir)

        with os.fdopen(fd, "w") as tmp_file:
            tmp_file.write(json.dumps(file_md))

        return (md_dir, md_file)

    def _prepare_dataset(self, host, secret_key, resource):
        located_files = []
        missing_files = []
        tmp_files_created = []
        tmp_dirs_created = []

        # first check if any files in dataset accessible locally
        ds_file_list = pyclowder.datasets.get_file_list(self, host, secret_key, resource["id"])
        for ds_file in ds_file_list:
            file_path = self._check_for_local_file(host, secret_key, ds_file)
            if not file_path:
                missing_files.append(ds_file)
            else:
                # Also get file metadata in format expected by extrator
                (file_md_dir, file_md_tmp) = self._download_file_metadata(host, secret_key, ds_file['id'],
                                                                          ds_file['filepath'])
                located_files.append(file_path)
                located_files.append(file_md_tmp)
                tmp_files_created.append(file_md_tmp)
                tmp_dirs_created.append(file_md_dir)

        # If only some files found locally, check & download any that were missed
        if len(located_files) > 0:
            for ds_file in missing_files:
                # Download file to temp directory
                inputfile = pyclowder.files.download(self, host, secret_key, ds_file['id'], ds_file['id'],
                                                     ds_file['file_ext'])
                # Also get file metadata in format expected by extractor
                (file_md_dir, file_md_tmp) = self._download_file_metadata(host, secret_key, ds_file['id'],
                                                                          ds_file['filepath'])
                located_files.append(inputfile)
                located_files.append(file_md_tmp)
                tmp_files_created.append(inputfile)
                tmp_files_created.append(file_md_tmp)
                tmp_dirs_created.append(file_md_dir)

            # Also, get dataset metadata (normally included in dataset .zip download file)
            ds_md = pyclowder.datasets.download_metadata(self, host, secret_key, resource["id"])
            md_name = "%s_dataset_metadata.json" % resource["id"]
            md_dir = tempfile.mkdtemp(suffix=resource["id"])
            (fd, md_file) = tempfile.mkstemp(suffix=md_name, dir=md_dir)
            with os.fdopen(fd, "w") as tmp_file:
                tmp_file.write(json.dumps(ds_md))
            located_files.append(md_file)
            tmp_files_created.append(md_file)
            tmp_dirs_created.append(md_dir)

            file_paths = located_files

        # If we didn't find any files locally, download dataset .zip as normal
        else:
            inputzip = pyclowder.datasets.download(self, host, secret_key, resource["id"])
            file_paths = pyclowder.utils.extract_zip_contents(inputzip)
            tmp_files_created += file_paths
            tmp_files_created.append(inputzip)

        return (file_paths, tmp_files_created, tmp_dirs_created)

    # pylint: disable=too-many-branches,too-many-statements
    def _process_message(self, body):
        """The actual processing of the message.

        This will register the extractor with the clowder instance that the message came from.
        Next it will call check_message to see if the message should be processed and if the
        file should be downloaded. Finally it will call the actual process_message function.
        """

        logger = logging.getLogger(__name__)

        host = body.get('host', '')
        if host == '':
            return
        elif not host.endswith('/'):
            host += '/'
        secret_key = body.get('secretKey', '')
        retry_count = 0 if 'retry_count' not in body else body['retry_count']
        resource = self._build_resource(body, host, secret_key)
        if not resource:
            return

        # register extractor
        url = "%sapi/extractors" % host
        if url not in Connector.registered_clowder:
            Connector.registered_clowder.append(url)
            self.register_extractor("%s?key=%s" % (url, secret_key))

        # tell everybody we are starting to process the file
        self.status_update(pyclowder.utils.StatusMessage.start, resource, "Started processing")

        # checks whether to process the file in this message or not
        # pylint: disable=too-many-nested-blocks
        try:
            check_result = pyclowder.utils.CheckMessage.download
            if self.check_message:
                check_result = self.check_message(self, host, secret_key, resource, body)
            if check_result != pyclowder.utils.CheckMessage.ignore:
                if self.process_message:

                    # FILE MESSAGES ---------------------------------------
                    if resource["type"] == "file":
                        file_path = None
                        found_local = False
                        try:
                            if check_result != pyclowder.utils.CheckMessage.bypass:
                                file_metadata = pyclowder.files.download_info(self, host, secret_key, resource["id"])
                                file_path = self._check_for_local_file(host, secret_key, file_metadata)
                                if not file_path:
                                    file_path = pyclowder.files.download(self, host, secret_key, resource["id"],
                                                                         resource["intermediate_id"],
                                                                         resource["file_ext"])
                                else:
                                    found_local = True
                                resource['local_paths'] = [file_path]

                            self.process_message(self, host, secret_key, resource, body)
                        finally:
                            if file_path is not None and not found_local:
                                try:
                                    os.remove(file_path)
                                except OSError:
                                    logger.exception("Error removing download file")

                    # DATASET/METADATA MESSAGES ---------------------------------------
                    else:
                        file_paths, tmp_files, tmp_dirs = [], [], []
                        try:
                            if check_result != pyclowder.utils.CheckMessage.bypass:
                                (file_paths, tmp_files, tmp_dirs) = self._prepare_dataset(host, secret_key, resource)
                            resource['local_paths'] = file_paths

                            self.process_message(self, host, secret_key, resource, body)
                        finally:
                            for tmp_f in tmp_files:
                                try:
                                    os.remove(tmp_f)
                                except OSError:
                                    logger.exception("Error removing temporary dataset file")
                            for tmp_d in tmp_dirs:
                                try:
                                    os.rmdir(tmp_d)
                                except OSError:
                                    logger.exception("Error removing temporary dataset directory")

            else:
                self.status_update(pyclowder.utils.StatusMessage.processing, resource, "Skipped in check_message")

            self.message_ok(resource)

        except SystemExit as exc:
            status = "sys.exit : " + exc.message
            logger.exception("[%s] %s", resource['id'], status)
            self.status_update(pyclowder.utils.StatusMessage.error, resource, status)
            self.message_resubmit(resource, retry_count)
            raise
        except KeyboardInterrupt:
            status = "keyboard interrupt"
            logger.exception("[%s] %s", resource['id'], status)
            self.status_update(pyclowder.utils.StatusMessage.error, resource, status)
            self.message_resubmit(resource, retry_count)
            raise
        except GeneratorExit:
            status = "generator exit"
            logger.exception("[%s] %s", resource['id'], status)
            self.status_update(pyclowder.utils.StatusMessage.error, resource, status)
            self.message_resubmit(resource, retry_count)
            raise
        except StandardError as exc:
            status = "standard error : " + str(exc.message)
            logger.exception("[%s] %s", resource['id'], status)
            self.status_update(pyclowder.utils.StatusMessage.error, resource, status)
            if retry_count < 10:
                self.message_resubmit(resource, retry_count+1)
            else:
                self.message_error(resource)
        except subprocess.CalledProcessError as exc:
            status = str.format("Error processing [exit code={}]\n{}", exc.returncode, exc.output)
            logger.exception("[%s] %s", resource['id'], status)
            self.status_update(pyclowder.utils.StatusMessage.error, resource, status)
            self.message_error(resource)
        except Exception as exc:  # pylint: disable=broad-except
            status = "Error processing : " + exc.message
            logger.exception("[%s] %s", resource['id'], status)
            self.status_update(pyclowder.utils.StatusMessage.error, resource, status)
            self.message_error(resource)

    def register_extractor(self, endpoints):
        """Register extractor info with Clowder.

        This assumes a file called extractor_info.json to be located in either the
        current working directory, or the folder where the main program is started.
        """

        # don't do any work if we wont register the endpoint
        if not endpoints or endpoints == "":
            return

        logger = logging.getLogger(__name__)

        headers = {'Content-Type': 'application/json'}
        data = self.extractor_info

        for url in endpoints.split(','):
            if url not in Connector.registered_clowder:
                Connector.registered_clowder.append(url)
                try:
                    result = requests.post(url.strip(), headers=headers,
                                           data=json.dumps(data),
                                           verify=self.ssl_verify)
                    result.raise_for_status()
                    logger.debug("Registering extractor with %s : %s", url, result.text)
                except Exception as exc:  # pylint: disable=broad-except
                    logger.exception('Error in registering extractor: ' + str(exc))

    # pylint: disable=no-self-use
    def status_update(self, status, resource, message):
        """Sends a status message.

        These messages, unlike logger messages, will often be send back to clowder to let
        the instance know the progress of the extractor.

        Keyword arguments:
        status - START | PROCESSING | DONE | ERROR
        resource  - descriptor object with {"type", "id"} fields
        message - contents of the status update
        """
        logging.getLogger(__name__).info("[%s] : %s: %s", resource["id"], status, message)

    def message_ok(self, resource):
        self.status_update(pyclowder.utils.StatusMessage.done, resource, "Done processing")

    def message_error(self, resource):
        self.status_update(pyclowder.utils.StatusMessage.error, resource, "Error processing message")

    def message_resubmit(self, resource, retry_count):
        self.status_update(pyclowder.utils.StatusMessage.processing, resource, "Resubmitting message (attempt #%s)"
                           % retry_count)

    def get(self, url, params=None, raise_status=True, **kwargs):
        """
        This methods wraps the Python requests GET method
        :param url: URl to use in GET request
        :param params: (optional) GET request parameters
        :param raise_status: (optional) If set to True, call raise_for_status. Default is True.
        :param kwargs: List of other optional arguments to pass to GET call
        :return: Response of the GET request
        """

        response = requests.get(url, params=params, **kwargs)
        if raise_status:
            response.raise_for_status()

        return response

    def post(self, url, data=None, json_data=None, raise_status=True, **kwargs):
        """
        This methods wraps the Python requests POST method
        :param url: URl to use in POST request
        :param data: (optional) data (Dictionary, bytes, or file-like object) to send in the body of POST request
        :param json_data: (optional) json data to send with POST request
        :param raise_status: (optional) If set to True, call raise_for_status. Default is True.
        :param kwargs: List of other optional arguments to pass to POST call
        :return: Response of the POST request
        """

        response = requests.post(url, data=data, json=json_data, **kwargs)
        if raise_status:
            response.raise_for_status()

        return response

    def put(self, url, data=None, raise_status=True, **kwargs):
        """
        This methods wraps the Python requests PUT method
        :param url: URl to use in PUT request
        :param data: (optional) data to send with PUT request
        :param raise_status: (optional) If set to True, call raise_for_status. Default is True.
        :param kwargs: List of other optional arguments to pass to PUT call
        :return: Response of the PUT request
        """

        response = requests.put(url, data=data, **kwargs)
        if raise_status:
            response.raise_for_status()

        return response

    def delete(self, url, raise_status=True, **kwargs):
        """
        This methods wraps the Python requests DELETE method
        :param url: URl to use in DELETE request
        :param raise_status: (optional) If set to True, call raise_for_status. Default is True.
        :param kwargs: List of other optional arguments to pass to DELETE call
        :return: Response of the DELETE request
        """

        response = requests.delete(url, **kwargs)
        if raise_status:
            response.raise_for_status()

        return response


# pylint: disable=too-many-instance-attributes
class RabbitMQConnector(Connector):
    """Listens for messages on RabbitMQ.

    This will connect to rabbitmq and register the extractor with a queue. If the exchange
    and key are specified it will bind the exchange to the queue. If an exchange is
    specified it will always try to bind the special key extractors.<extractor_info[name]> to the
    exchange and queue.
    """

    # pylint: disable=too-many-arguments
    def __init__(self, extractor_info, rabbitmq_uri, rabbitmq_exchange=None, rabbitmq_key=None,
                 check_message=None, process_message=None, ssl_verify=True, mounted_paths=None):
        Connector.__init__(self, extractor_info, check_message, process_message, ssl_verify, mounted_paths)
        self.rabbitmq_uri = rabbitmq_uri
        self.rabbitmq_exchange = rabbitmq_exchange
        self.rabbitmq_key = rabbitmq_key
        self.channel = None
        self.connection = None
        self.consumer_tag = None
        self.worker = None

    def connect(self):
        """connect to rabbitmq using URL parameters"""

        parameters = pika.URLParameters(self.rabbitmq_uri)
        self.connection = pika.BlockingConnection(parameters)

        # connect to channel
        self.channel = self.connection.channel()

        # setting prefetch count to 1 so we only take 1 message of the bus at a time,
        # so other extractors of the same type can take the next message.
        self.channel.basic_qos(prefetch_count=1)

        # declare the queue in case it does not exist
        self.channel.queue_declare(queue=self.extractor_info['name'], durable=True)
        self.channel.queue_declare(queue='error.'+self.extractor_info['name'], durable=True)

        # register with an exchange
        if self.rabbitmq_exchange:
            # declare the exchange in case it does not exist
            self.channel.exchange_declare(exchange=self.rabbitmq_exchange, exchange_type='topic',
                                          durable=True)

            # connect queue and exchange
            if self.rabbitmq_key:
                if isinstance(self.rabbitmq_key, str):
                    self.channel.queue_bind(queue=self.extractor_info['name'],
                                            exchange=self.rabbitmq_exchange,
                                            routing_key=self.rabbitmq_key)
                else:
                    for key in self.rabbitmq_key:
                        self.channel.queue_bind(queue=self.extractor_info['name'],
                                                exchange=self.rabbitmq_exchange,
                                                routing_key=key)

            self.channel.queue_bind(queue=self.extractor_info['name'],
                                    exchange=self.rabbitmq_exchange,
                                    routing_key="extractors." + self.extractor_info['name'])

    def listen(self):
        """Listen for messages coming from RabbitMQ"""

        # check for connection
        if not self.channel:
            self.connect()

        # create listener
        self.consumer_tag = self.channel.basic_consume(self.on_message, queue=self.extractor_info['name'],
                                                       no_ack=False)

        # start listening
        logging.getLogger(__name__).info("Starting to listen for messages.")
        try:
            # pylint: disable=protected-access
            while self.channel and self.channel._consumer_infos:
                self.channel.connection.process_data_events(time_limit=1)  # 1 second
                if self.worker:
                    self.worker.process_messages(self.channel)
                    if self.worker.is_finished():
                        self.worker = None
        except SystemExit:
            raise
        except KeyboardInterrupt:
            raise
        except GeneratorExit:
            raise
        except Exception:  # pylint: disable=broad-except
            logging.getLogger(__name__).exception("Error while consuming messages.")
        finally:
            logging.getLogger(__name__).info("Stopped listening for messages.")
            if self.channel:
                try:
                    self.channel.close()
                except Exception:
                    logging.getLogger(__name__).exception("Error while closing channel.")
                finally:
                    self.channel = None
            if self.connection:
                try:
                    self.connection.close()
                except Exception:
                    logging.getLogger(__name__).exception("Error while closing connection.")
                finally:
                    self.connection = None

    def stop(self):
        """Tell the connector to stop listening for messages."""
        if self.channel:
            self.channel.stop_consuming(self.consumer_tag)

    def alive(self):
        return self.connection is not None

    def on_message(self, channel, method, header, body):
        """When the message is received this will call the generic _process_message in
        the connector class. Any message will only be acked if the message is processed,
        or there is an exception (except for SystemExit and SystemError exceptions).
        """

        json_body = json.loads(body)
        if 'routing_key' not in json_body and method.routing_key:
            json_body['routing_key'] = method.routing_key

        self.worker = RabbitMQHandler(self.extractor_info, self.check_message, self.process_message,
                                      self.ssl_verify, self.mounted_paths, method, header, body)
        self.worker.start_thread(json_body)


class RabbitMQHandler(Connector):
    """Simple handler that will process a single message at a time.

    To avoid sharing non-threadsafe channels across threads, this will maintain
    a queue of messages that the super- loop can access and send later.
    """

    def __init__(self, extractor_info, check_message=None, process_message=None, ssl_verify=True,
                 mounted_paths=None, method=None, header=None, body=None):
        Connector.__init__(self, extractor_info, check_message, process_message, ssl_verify, mounted_paths)
        self.method = method
        self.header = header
        self.body = body
        self.messages = []
        self.thread = None
        self.finished = False
        self.lock = threading.Lock()

    def start_thread(self, json_body):
        """Start the separate thread for processing & create a queue for messages.

        messages is a list of message objects:
        {
            "type": status/ok/error/resubmit
            "resource": resource
            "status": status (status_update only)
            "message": message content (status_update only)
            "retry_count": retry_count (message_resubmit only)
        }
        """
        self.thread = threading.Thread(target=self._process_message, args=(json_body,))
        self.thread.start()

    def is_finished(self):
        with self.lock:
            return self.thread and not self.thread.isAlive() and self.finished and len(self.messages) == 0

    def process_messages(self, channel):
        while self.messages:
            with self.lock:
                msg = self.messages.pop(0)

            if msg["type"] == 'status':
                if self.header.reply_to:
                    properties = pika.BasicProperties(delivery_mode=2, correlation_id=self.header.correlation_id)
                    channel.basic_publish(exchange='',
                                          routing_key=self.header.reply_to,
                                          properties=properties,
                                          body=json.dumps(msg['status']))

            elif msg["type"] == 'ok':
                channel.basic_ack(self.method.delivery_tag)
                with self.lock:
                    self.finished = True

            elif msg["type"] == 'error':
                properties = pika.BasicProperties(delivery_mode=2, reply_to=self.header.reply_to)
                channel.basic_publish(exchange='',
                                      routing_key='error.' + self.extractor_info['name'],
                                      properties=properties,
                                      body=self.body)
                channel.basic_ack(self.method.delivery_tag)
                with self.lock:
                    self.finished = True

            elif msg["type"] == 'resubmit':
                retry_count = msg['retry_count']
                queue = self.extractor_info['name']
                properties = pika.BasicProperties(delivery_mode=2, reply_to=self.header.reply_to)
                jbody = json.loads(self.body)
                jbody['retry_count'] = retry_count
                if 'exchange' not in jbody and self.method.exchange:
                    jbody['exchange'] = self.method.exchange
                if 'routing_key' not in jbody and self.method.routing_key and self.method.routing_key != queue:
                    jbody['routing_key'] = self.method.routing_key
                channel.basic_publish(exchange='',
                                      routing_key=queue,
                                      properties=properties,
                                      body=json.dumps(jbody))
                channel.basic_ack(self.method.delivery_tag)
                with self.lock:
                    self.finished = True

            else:
                logging.getLogger(__name__).error("Received unknown message type [%s]." % msg["type"])

    def status_update(self, status, resource, message):
        super(RabbitMQHandler, self).status_update(status, resource, message)
        status_report = dict()
        # TODO: Update this to check resource["type"] once Clowder better supports dataset events
        status_report['file_id'] = resource["id"]
        status_report['extractor_id'] = self.extractor_info['name']
        status_report['status'] = "%s: %s" % (status, message)
        status_report['start'] = pyclowder.utils.iso8601time()
        with self.lock:
            self.messages.append({"type": "status",
                                  "status": status_report,
                                  "resource": resource,
                                  "message": message})

    def message_ok(self, resource):
        super(RabbitMQHandler, self).message_ok(resource)
        with self.lock:
            self.messages.append({"type": "ok"})

    def message_error(self, resource):
        super(RabbitMQHandler, self).message_error(resource)
        with self.lock:
            self.messages.append({"type": "error"})

    def message_resubmit(self, resource, retry_count):
        super(RabbitMQHandler, self).message_resubmit(resource, retry_count)
        with self.lock:
            self.messages.append({"type": "resubmit", "retry_count": retry_count})


class HPCConnector(Connector):
    """Takes pickle files and processes them."""

    # pylint: disable=too-many-arguments
    def __init__(self, extractor_info, picklefile,
                 check_message=None, process_message=None, ssl_verify=True, mounted_paths=None):
        Connector.__init__(self, extractor_info, check_message, process_message, ssl_verify, mounted_paths)
        self.picklefile = picklefile
        self.logfile = None

    def listen(self):
        """Reads the picklefile, sets up the logfile and call _process_message."""
        if isinstance(self.picklefile, str):
            try:
                with open(self.picklefile, 'rb') as pfile:
                    body = pickle.load(pfile)
                    self.logfile = body['logfile']
                    self._process_message(body)
            finally:
                self.logfile = None
        else:
            for onepickle in self.picklefile:
                try:
                    with open(onepickle, 'rb') as pfile:
                        body = pickle.load(pfile)
                        self.logfile = body['logfile']
                        self._process_message(body)
                finally:
                    self.logfile = None

    def alive(self):
        return self.logfile is not None

    def status_update(self, status, resource, message):
        """Store notification on log file with update"""

        logger = logging.getLogger(__name__)
        logger.debug("[%s] : %s : %s", resource["id"], status, message)

        if self.logfile and os.path.isfile(self.logfile) is True:
            try:
                with open(self.logfile, 'a') as log:
                    statusreport = dict()
                    statusreport['file_id'] = resource["id"]
                    statusreport['extractor_id'] = self.extractor_info['name']
                    statusreport['status'] = "%s: %s" % (status, message)
                    statusreport['start'] = time.strftime('%Y-%m-%dT%H:%M:%S')
                    log.write(json.dumps(statusreport) + '\n')
            except:
                logger.exception("Error: unable to write extractor status to log file")
                raise


class LocalConnector(Connector):
    """
    Class that will handle processing of files locally. Needed for Big Data support.

    This will get the file to be processed from environment variables

    """

    def __init__(self, extractor_info, input_file_path, process_message=None, output_file_path=None):
        super(LocalConnector, self).__init__(extractor_info, process_message=process_message)
        self.input_file_path = input_file_path
        self.output_file_path = output_file_path
        self.completed_processing = False

    def listen(self):
        local_parameters = dict()
        local_parameters["inputfile"] = self.input_file_path
        local_parameters["outputfile"] = self.output_file_path

        # Set other parameters to emtpy string
        local_parameters["fileid"] = None
        local_parameters["id"] = None
        local_parameters["host"] = None
        local_parameters["intermediateId"] = None
        local_parameters["fileSize"] = None
        local_parameters["flags"] = None
        local_parameters["filename"] = None
        local_parameters["logfile"] = None
        local_parameters["datasetId"] = None
        local_parameters["secretKey"] = None
        local_parameters["routing_key"] = None

        ext = os.path.splitext(self.input_file_path)[1]
        resource = {
            "type": "file",
            "id": "",
            "intermediate_id": "",
            "name": self.input_file_path,
            "file_ext": ext,
            "parent": dict(),
            "local_paths": [self.input_file_path]
        }

        # TODO: BD-1638 Call _process_message by generating pseudo JSON responses from get method
        self.process_message(self, "", "", resource, local_parameters)
        self.completed_processing = True

    def alive(self):
        return not self.completed_processing

    def stop(self):
        pass

    def get(self, url, params=None, raise_status=True, **kwargs):
        logging.getLogger(__name__).debug("GET: " + url)
        return None

    def post(self, url, data=None, json_data=None, raise_status=True, **kwargs):

        logging.getLogger(__name__).debug("POST: " + url)
        # Handle metadata POST endpoints
        if url.find("/technicalmetadatajson") != -1 or url.find("/metadata.jsonld") != -1:

            json_metadata_formatted_string = json.dumps(json.loads(data), indent=4, sort_keys=True)
            logging.getLogger(__name__).debug(json_metadata_formatted_string)
            extension = ".json"

            # If output file path is not set
            if self.output_file_path is None or self.output_file_path == "":
                # Create json filename from the input filename
                json_filename = self.input_file_path + extension
            else:
                json_filename = str(self.output_file_path)
                if not json_filename.endswith(extension):
                    json_filename += extension

            # Checking permissions using EAFP (Easier to Ask for Forgiveness than Permission) technique
            try:
                json_file = open(json_filename, "w")
            except IOError as e:
                if e.errno == errno.EACCES:
                    logging.getLogger(__name__).exception(
                        "You do not have enough permissions to create the output file " + json_filename)
                else:
                    raise
            else:
                with json_file:
                    json_file.write(json_metadata_formatted_string)
                    logging.getLogger(__name__).debug("Metadata output file path: " + json_filename)

    def put(self, url, data=None, raise_status=True, **kwargs):
        logging.getLogger(__name__).debug("PUT: " + url)
        return None

    def delete(self, url, raise_status=True, **kwargs):
        logging.getLogger(__name__).debug("DELETE: " + url)
        return None
