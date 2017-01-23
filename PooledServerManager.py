from pika import adapters
import pika
import logging
import uuid
from tornado.concurrent import Future
from tornado.ioloop import PeriodicCallback
import random
import copy
import hashlib
import json
import os
from datetime import datetime, timedelta
from database_objects import Base, ManagerSettings, Settings, RenderJob, Frame, SceneTypes, create_all
from sqlalchemy.orm.exc import MultipleResultsFound,NoResultFound
from zipfile import ZipFile
import glob
from sqlalchemy import or_

LOG_FORMAT = ('%(levelname) -10s %(asctime)s %(name) -30s %(funcName) '
              '-35s %(lineno) -5d: %(message)s')
LOGGER = logging.getLogger(__name__)

class PooledServerManager(object):


    EXCHANGE = 'message'
    EXCHANGE_TYPE = 'topic'
    QUEUE = 'text'
    ROUTING_KEY = 'example.text'

    def __init__(self, io_loop, db, **kw_args):
        self._io_loop = io_loop
        self._db = db
        self._data_path = kw_args["data_path"]
        self._scene_path = kw_args["scene_path"]
        self._connection = None
        self._channel = None
        self._closing = False
        self._status_consumer_tag = None
        self._reject_consumer_tag = None
        self._url = kw_args["amqp_url"]
        self._kill_on_start = kw_args["kill_jobs_on_start"]
        self._render_queue = "render_queue"
        self._status_queue = "dl"
        self._reject_queue = "reject"
        self._worker_status_queue = "worker_status"
        self._outstanding_messages = []
        self._workers = []

        self.task_update = PeriodicCallback( self.task_update, 5000, self._io_loop );
        self.task_update.start()

    def connect(self):
        LOGGER.info('Connecting to %s', self._url)
        self._connection =  adapters.TornadoConnection(
            pika.URLParameters(self._url),
            on_open_callback=self.on_connection_open,
            on_open_error_callback=self.on_connection_error,
            custom_ioloop=self._io_loop)

    def close_connection(self):
        LOGGER.info('Closing connection')
        self._connection.close()

    def add_on_connection_close_callback(self):
        LOGGER.info('Adding connection close callback')
        self._connection.add_on_close_callback(self.on_connection_closed)

    def on_connection_error(self, connection, error_message):
        LOGGER.warning('Connection failed, trying again in 5 seconds: %s',
                       error_message)
        self._io_loop.call_later(5, self.connect)

    def on_connection_closed(self, connection, reply_code, reply_text):
        self._channel = None
        if self._closing:
            pass
        #    self._connection.ioloop.stop()
        else:
            LOGGER.warning('Connection closed, reopening in 5 seconds: (%s) %s',
                           reply_code, reply_text)
            self._connection.add_timeout(5, self.reconnect)

    def on_connection_open(self, unused_connection):
        LOGGER.info('Connection opened')
        self._connection = unused_connection
        self.add_on_connection_close_callback()
        self.open_channel()

    def reconnect(self):
        if not self._closing:
            self._connection = self.connect()

    def add_on_channel_close_callback(self):
        LOGGER.info('Adding channel close callback')
        self._channel.add_on_close_callback(self.on_channel_closed)

    def on_channel_closed(self, channel, reply_code, reply_text):
        LOGGER.warning('Channel %i was closed: (%s) %s',
                       channel, reply_code, reply_text)
        self._connection.close()

    def on_channel_open(self, channel):
        LOGGER.info('Channel opened')
        self._channel = channel                                
        self.add_on_channel_close_callback()
        self.setup_exchange(self.EXCHANGE)


    def setup_exchange(self, exchange_name):
        LOGGER.info('Declaring exchanges')
 
        self.on_exchange_declareok(None)

    def on_exchange_declareok(self, unused_frame):
        LOGGER.info('Exchange declared')
        self.setup_queue(self.QUEUE)

    def setup_queue(self, queue_name):
        LOGGER.info('Declaring queue %s', queue_name)
        self._channel.queue_declare(callback=self.on_render_queue_declareok,queue="log_queue",
                                    durable=True, exclusive=False, auto_delete=False)
        
        self.start_consuming();

    def on_render_queue_declareok(self, method_frame):
        LOGGER.info('Binding %s to %s with %s',
                    self.EXCHANGE, self.QUEUE, self.ROUTING_KEY)
        

    def add_on_cancel_callback(self):
        LOGGER.info('Adding consumer cancellation callback')
        self._channel.add_on_cancel_callback(self.on_consumer_cancelled)

    def on_consumer_cancelled(self, method_frame):
        LOGGER.info('Consumer was cancelled remotely, shutting down: %r',
                    method_frame)
        if self._channel:
            self._channel.close()


    def acknowledge_message(self, delivery_tag):
        self._channel.basic_ack(delivery_tag)

        
    def on_message(self, unused_channel, basic_deliver, properties, body):
        self.acknowledge_message(basic_deliver.delivery_tag)


    def reject_callback(self, ch, method, properties, body):
        print "Moving message from reject queue to status queue."
        self.acknowledge_message(method.delivery_tag)
        response_message = {}
        response_message["status"] = "REJECT"


    def task_update(self,):
        update_time = datetime.now()
       
        # Process all new jobs without frames generated yet...
        pending_jobs = self._db.query(RenderJob).filter_by(job_status=0).all()
        for job in pending_jobs:
            self._channel.queue_declare(callback=self.on_render_queue_declareok,queue='render_'+job.uuid,
                                        durable=True, exclusive=False, auto_delete=False)
            for f in range(job.frame_start,job.frame_end+1):
                job.frames.append(Frame(frame=f, status=0,uuid=str(uuid.uuid4())));
            job.job_status=1
            job.settings.priority = job.id;
        self._db.commit()

        # Frame Status codes
        # 0 - Unsubmitted
        # 1 - Waiting to render
        # 2 - Rendering (Will have start time set)
        # 3 - Done/File Wait (Will have end time set)
        # 4 - Complete
        # 5 - Failed
        
        # Commit all unqueued frames for rendering
        unqueued_frames = self._db.query(Frame).filter_by(status=0).all()
        for frame in unqueued_frames:
            message = {}
            message["frame"] = frame.frame
            message["scene"] = frame.job.scene
            message["uuid"] = frame.uuid
            message["command"] = "render"
            message["type"] = frame.job.type.type_id            
            
            status = self._channel.basic_publish(exchange='',
                                                 routing_key='render_'+frame.job.uuid,
                                                 body=json.dumps(message),
                                                 properties=pika.BasicProperties(
                                                     delivery_mode = 2, # make message persistent           
                                                 ))
            frame.status = 1            
        self._db.commit()
        
        # Collect all frames which are waiting on files
        filename = os.path.join( self._data_path, "frame_cache", "*" )
        matches = glob.glob( filename )
        for match in matches:
            base = os.path.basename( match )
            prefix, postfix = base.split('.', 1 )
            frame = self._db.query(Frame).filter(Frame.uuid==prefix).one_or_none()
            if frame != None:
                save_dir = os.path.join(self._data_path,  "completed_renders",frame.job.name+"_"+frame.job.uuid )
                try:
                    os.makedirs( save_dir  )
                except:
                    pass
                os.rename( match, os.path.join(save_dir, ("{:08d}".format(frame.frame))+"."+postfix ) )
                frame.status = 4
        self._db.commit()
        
        # Check to see if a job has moved from active to completed, also update eta
        active_jobs = self._db.query(RenderJob).filter_by(job_status=1).all()
        for job in active_jobs:
            times = self._db.query(Frame.start,Frame.end).join(RenderJob).filter(RenderJob.id==job.id).filter(Frame.status==4).all()
            waiting_count = self._db.query(Frame.start,Frame.end).join(RenderJob).filter(RenderJob.id==job.id).filter(Frame.status<4).count()
            for i in range(len(times)):
                times[i] = list( times[i] )
                if times[i][0] == None:
                    times[i][0] = datetime.now()
                if times[i][1] == None:
                    times[i][1] = datetime.now()
                           
            if len(times) > 0:
                start_time = sorted( times, key=lambda x: x[0] )[0][0]
                end_time = sorted( times, key=lambda x: x[1] )[-1][1]
                
                #tstart, tend = zip(*times)
                #eta_d = timedelta()
                #for delta in map( lambda x,y: y-x,tstart,tend):
                #    eta_d = eta_d + delta
                eta_d = end_time - start_time;
                job.eta = datetime.now() + timedelta(seconds=eta_d.total_seconds()/len(times)) * waiting_count
            else:
                job.eta = datetime.now()
            if self._db.query(RenderJob).join(Frame).filter(RenderJob.id==job.id).filter(Frame.status==4).count() == (job.frame_end - job.frame_start + 1) :
                LOGGER.info("RenderJob " + job.uuid + " has been completed.")
                
                job.job_status = 2  

            # If try hard is enabled, check and requeue any failed frames
            if job.try_hard == 1:
                failed_frames = self._db.query(Frame).join(RenderJob).filter(RenderJob.id==job.id).filter(Frame.status==5).all()
                for failed_frame in failed_frames:
                    requeue( failed_frame )
                
                
                                              
        self._db.commit()           
                              
            
    def requeue( self, frame ):
        message = {}
        message["frame"] = frame.frame
        message["scene"] = frame.job.scene
        message["uuid"] = frame.uuid
        message["command"] = "render"
        message["type"] = frame.job.type.type_id
        
        status = self._channel.basic_publish(exchange='',
                                             routing_key='render_'+frame.job.uuid,
                                             body=json.dumps(message),
                                             properties=pika.BasicProperties(
                                                 delivery_mode = 2, # make message persistent           
                                            ))
        frame.status = 1
        frame.node = ""
        self._db.commit();


    def log_callback( self, ch, method, properties, body):
        message = json.loads( body );
        if message["event"] == "render_start":
            uuid = message["uuid"]
            try:
                frame = self._db.query(Frame).filter_by(uuid=uuid).one()
            except NoResultFound:
                LOGGER.warn("Render Start Event for unknown frame "+uuid )
            else:
                if frame.status == 1 or frame.status == 2:
                    frame.start = datetime(*message["time"])
                    frame.status = 2
                    frame.node = properties.app_id
                    LOGGER.info("Render Start Event recorded for frame " + uuid );
                else:
                    LOGGER.warn("Render Start Event was unexpected for frame " + uuid );
                self._db.commit()                
            
        elif message["event"] == "render_finish":
            uuid = message["uuid"]
            try:
                frame = self._db.query(Frame).filter_by(uuid=uuid).one()
            except NoResultFound:
                LOGGER.warn("Render Finish Event for unknown frame "+uuid )
            else:
                if frame.status == 2:
                    frame.end = datetime(*message["time"])
                    frame.status = 3
                    frame.node = properties.app_id
                    LOGGER.info("Render Finish Event recorded for frame " + uuid );
                else:
                    LOGGER.warn("Render Finish Event was unexpected for frame " + uuid );
                self._db.commit()
        elif message["event"] == "render_fail":
            uuid = message["uuid"]
            try:
                frame = self._db.query(Frame).filter_by(uuid=uuid).one()
            except NoResultFound:
                LOGGER.warn("Render Fail Event for unknown frame "+uuid )
            else:
                if frame.status == 2:
                    #frame.end = datetime(*message["time"])
                    frame.status = 5
                    frame.node = properties.app_id
                    LOGGER.info("Render Fail Event recorded for frame " + uuid );
                else:
                    LOGGER.warn("Render Fail Event was unexpected for frame " + uuid );
                self._db.commit()  
        else:
            LOGGER.warning( "Unknown log event of type '" + message["event"] + "' was seen!" );
        
        self.acknowledge_message(method.delivery_tag)

    def cancel_job(self, uuid ):
        try:
            job = self._db.query(RenderJob).filter(RenderJob.uuid==uuid).filter(RenderJob.job_status==1).one()
        except NoResultFound:
            pass
        else:
            for frame in job.frames:
                if frame.status < 2:
                    frame.status = -1
                else:
                    self._db.delete(frame);
            self._channel.queue_delete(queue='render_'+job.uuid)
            self._db.delete(job.settings)
            self._db.delete(job)
            self._db.commit()
        
    def render_prune_callback(self, ch, method, properties, body):
        message = json.loads(body);
        try:
            frame = self._db.query(Frame).filter(Frame.uuid==message["uuid"]).one()
        except NoResultFound:
            pass
        else:
            if frame.status == -1:
                ch.basic_ack(delivery_tag = method.delivery_tag);
                self._db.delete(frame)
            else:
                ch.basic_reject(delivery_tag = method.delivery_tag, requeue=True);
        self._db.commit()
        if self._db.query(Frame).filter(Frame.status==-1).count() == 0:
            self._channel.basic_cancel( consumer_tag="frame_pruner" )

            
    def validate_job(self, submitter, email, name, scene, frame_start, frame_end, job_type):
        class validate_status:
            def __init__(self, db, scenes_path, scene_name, job_type):
                try:
                    jobType = db.query(SceneTypes).filter(SceneTypes.type_id==job_type).one()
                except NoResultFound:
                    self.good=False
                    self.err_msg="Job type is not recognized."
                else:
                    if jobType.signatureFile:
                        if len(glob.glob( os.path.join( scenes_path, scene_name, jobType.signatureFile ) )):
                            self.good=True
                            self.err_msg="success"
                        else:
                            self.good=False
                            self.err_msg="Scene path is not valid."
                    else:
                        self.good=True
                        self.err_msg="success"
                            
        return validate_status(self._db, self._scene_path, scene, job_type)

    def submit_job(self, job_record):
        job_record.job_status = 0
        job_record.uuid = str(uuid.uuid4())
        job_record.eta = datetime.now()
        job_record.start = datetime.now()
        job_record.end = datetime.now()
        #self._db.add(job_record);
                
        job_settings = Settings()
        job_settings.job = job_record
        job_settings.timeout = self._db.query(ManagerSettings).first().timeout;
        job_settings.retries = self._db.query(ManagerSettings).first().retries;
        #self._db.add(job_settings)

        self._db.add_all( [ job_record, job_settings ] )
        try:
            
            self._db.commit();
        except Exception, e:
            print e
            self._db.rollback();
            return False
        else:
            return True
        

    def on_cancelok(self, unused_frame):
        LOGGER.info('RabbitMQ acknowledged the cancellation of the consumer')
        self.close_channel()

    def null_result( self, unused_frame ):
        pass


    def stop_consuming(self):
        if self._channel:
            LOGGER.info('Sending a Basic.Cancel RPC command to RabbitMQ')
            #self._channel.basic_cancel(self.null_result, self._status_consumer_tag)
            #self._channel.basic_cancel(self.on_cancelok, self._reject_consumer_tag)

    def start_consuming(self):
        LOGGER.info('Issuing consumer related RPC commands')
        self.add_on_cancel_callback()
        self._channel.basic_consume( self.log_callback,
                                     'log_queue' )


    def on_bindok(self, unused_frame):
        LOGGER.info('Queue bound')

    def close_channel(self):
        LOGGER.info('Closing the channel')
        self._channel.close()

    def open_channel(self):
        LOGGER.info('Creating a new channel')
        self._connection.channel(on_open_callback=self.on_channel_open)
        
    
