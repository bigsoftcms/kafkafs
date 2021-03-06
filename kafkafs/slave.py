from uuid import UUID, getnode
import logging
import os
import errno

import six

from pykafka import KafkaClient

from kafkafs.fuse_pb2 import FuseChange


logger = logging.getLogger(__name__)


CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_TRUNC


class Slave():

    # operations which don't do fsync should not be committed
    COMMIT_IGNORE_OPS = [
        FuseChange.OPEN,
        FuseChange.WRITE,
    ]

    def __init__(self, filemanager, broker, topic, futures=None,
                 fetch_max_wait_ms=10):
        self.fm = filemanager
        self.broker = broker
        self.topic = topic
        self.futures = {} if futures is None else futures
        self.fetch_max_wait_ms = fetch_max_wait_ms

    def run(self):
        self.client = KafkaClient(hosts=self.broker)
        topic = self.client.topics[self.topic]
        consumer_group = six.b('%s:%s' % (getnode(), self.fm.root))
        consumer = topic.get_simple_consumer(
            consumer_group,
            use_rdkafka=True,
        )

        logger.info("Started kafkafs slave on %s", self.fm.root)

        for kafka_msg in consumer:

            msg = FuseChange.FromString(kafka_msg.value)
            logger.debug("%s", msg)

            try:
                ret = getattr(self, FuseChange.Operation.Name(msg.op))(msg)
                if msg.op not in self.COMMIT_IGNORE_OPS:
                    consumer.commit_offsets()
            except KeyError:
                continue
            except OSError as e:
                logging.error("Can't perform %s(%s)",
                              FuseChange.Operation.Name(msg.op),
                              UUID(bytes=msg.uuid),
                              exc_info=True)
                if msg.uuid in self.futures:
                    self.futures[msg.uuid].set_exception(e)
                raise

            if msg.uuid in self.futures:
                self.futures[msg.uuid].set_result(ret)

    def p(self, path):
        return self.fm.p(path)

    def CHMOD(self, msg):
        return os.chmod(self.p(msg.path), msg.mode)

    def CHOWN(self, msg):
        return os.chown(self.p(msg.path), msg.uid, msg.gid)

    def CREATE(self, msg):
        return self.fm.open(msg.uuid, msg.path, CREATE_FLAGS, msg.mode)

    def FSYNC(self, msg):
        if msg.fh_uuid not in self.fm:
            logger.error("FSYNC on not opened file %s:%s",
                         UUID(bytes=msg.fh_uuid), msg.path)
            raise OSError(errno.EBADF)
        fh = self.fm[msg.fh_uuid].fh
        if msg.datasync:
            return os.fdatasync(fh)
        else:
            return os.fsync(fh)

    def LINK(self, msg):
        return os.link(self.p(msg.src), self.p(msg.path))

    def MKDIR(self, msg):
        return os.mkdir(self.p(msg.path), msg.mode)

    def OPEN(self, msg):
        return self.fm.open(msg.uuid, msg.path, msg.flags, msg.mode)

    def RELEASE(self, msg):
        fh = self.fm[msg.fh_uuid].fh
        del self.fm[msg.fh_uuid]
        return os.close(fh)

    def RMDIR(self, msg):
        return os.rmdir(self.p(msg.path))

    def SYMLINK(self, msg):
        return os.symlink(msg.src, self.p(msg.path))

    def TRUNCATE(self, msg):
        with open(self.p(msg.path), 'r+') as f:
            return f.truncate(msg.length)

    def UNLINK(self, msg):
        return os.unlink(self.p(msg.path))

    def UTIME(self, msg):
        return os.utime(self.p(msg.path), (msg.atime, msg.mtime))

    def WRITE(self, msg):
        if msg.fh_uuid not in self.fm:
            # XXX: need to be rewinded to offset, where file was opened?
            # or just ignore O_CREATE would be enought?
            flags = list(msg.flags)
            flags.remove(FuseChange.O_CREAT)
            flags.remove(FuseChange.O_APPEND)
            flags.remove(FuseChange.O_TRUNC)
            self.fm.open(msg.uuid, msg.path, msg.flags, msg.mode)
        filehandle = self.fm[msg.fh_uuid]
        with filehandle.lock:
            os.lseek(filehandle.fh, msg.offset, 0)
            # XXX: what if returned less than len(msg.data)??
            return os.write(filehandle.fh, msg.data)
