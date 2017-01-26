import tornado.ioloop
import tornado.web
import tornado.websocket
from tornado.concurrent import Future
from tornado import gen
import string
import os
import os.path as osp
import json
import time
from datetime import date, datetime, timedelta
import subprocess
import random
import copy
import hashlib
import sys
import io
from tornado.options import define, options, parse_command_line
import resource
from zipfile import ZipFile
import ui_methods
from sqlalchemy import create_engine, and_, or_, desc
from sqlalchemy.orm import sessionmaker,scoped_session
from database_objects import Base, RenderJob, Frame, SceneTypes, Settings, Image, create_all
from PooledServerManager import PooledServerManager as ServerManager
from sqlalchemy.orm.exc import MultipleResultsFound,NoResultFound
import argparse
from requests_toolbelt import MultipartDecoder
from wand.image import Image as WandImage
import glob

import logging
LOG_FORMAT = ('%(levelname) -10s %(asctime)s %(name) -30s %(funcName) '
              '-35s %(lineno) -5d: %(message)s')
LOGGER = logging.getLogger(__name__)

class Application(tornado.web.Application):
    def __init__(self, engine, data_path, scene_path):
        self.data_path = data_path
        self.scene_path = scene_path

        handlers = [
            (r'/', IndexHandler),
            (r'/submit_job', JobSubmissionHandler),
            (r'/view_job', ViewJobHandler),
            (r'/upload_render', StoreHandler),
            (r'/fetch_render', FetchHandler),
            (r'/fetch_previewclip', FetchPreview),
            (r'/cancel_job', JobCancelHandler),
            (r'/available_jobs', JobListing),
            (r'/download_zip', DownloadZipHandler),
            #(r'/manual_upload', ManualStore),
            (r'/manual_requeue', ManualRequeue),
            (r'/toggle_auto_requeue', ToggleAutoRequeue),
            (r'/frame_settings', FrameSettings),
            (r'/make_priority', MakePriority),
        ]        
        settings = dict(
            static_path=os.path.join(os.path.dirname(__file__), "static"),
            cookie_secret="some_long_secret_and_other_settins",
            max_buffer_size=500*1024*1024,
        )
        tornado.web.Application.__init__(self, handlers, debug=True, ui_methods=ui_methods, **settings)
        # Have one global connection.
        self.db = scoped_session(sessionmaker(bind=engine))


class BaseHandler(tornado.web.RequestHandler):
    @property
    def db(self):
        return self.application.db
    @property
    def manager(self):
        return self.application.server_manager

    @property
    def data_path(self):
        return self.application.data_path

    @property
    def scene_path(self):
        return self.application.scene_path

    def getFrameOrError(self):
        frame_uuid = self.get_query_argument('uuid',default=None)
        frame_id = self.get_query_argument('id',default=None)
        if frame_uuid == None and frame_id == None:
            self.send_error(500)
            return None
        elif frame_uuid != None:
            try:            
                frame = self.db.query(Frame).filter_by(uuid=frame_uuid).one()
            except NoResultFound:
                self.send_error(500);
                return None
            else:
                return frame
        else:
            try:
                frame = self.db.query(Frame).filter_by(id=frame_id).one()
            except NoResultFound:
                self.send_error(500);
                return None
            else:
                return frame

    def getJobOrError(self):
        job_uuid = self.get_query_argument('uuid',default=None)
        job_id = self.get_query_argument('id',default=None)
        if job_uuid == None and job_id == None:
            self.send_error(500)
            return None
        elif job_uuid != None:
            try:            
                job = self.db.query(RenderJob).filter_by(uuid=job_uuid).one()
            except NoResultFound:
                self.send_error(500);
                return None
            else:
                return job
        else:
            try:
                job = self.db.query(RenderJob).filter_by(id=job_id).one()
            except NoResultFound:
                self.send_error(500);
                return None
            else:
                return job

            
class IndexHandler( BaseHandler ):
    @tornado.web.asynchronous
    def get(self):
        status_filter=int(self.get_argument("status",1))
        mode_map = ('pending','active','completed')
        if status_filter == 1:
            jobs=self.db.query(RenderJob).join(RenderJob.settings).filter(RenderJob.job_status==status_filter).order_by(Settings.priority).all()
        else:
            jobs=self.db.query(RenderJob).filter(RenderJob.job_status==status_filter).all()
            
        for j in jobs:
            j.p_f_complete = float(len([f for f in j.frames if f.status == 4]))/(j.frame_end - j.frame_start + 1)*100
            j.p_f_failed = float(len([f for f in j.frames if f.status == 5]))/(j.frame_end - j.frame_start + 1)*100
            
        params = {
            "uri":self.request.uri,
            "job_data":jobs,
            "mode":mode_map[status_filter],
            "now":datetime.now(),
            }
        
        self.render( "static/pages/index.html", **params )

class MakePriority( BaseHandler ):
    def get(self):
        job = self.getJobOrError()
        if job == None:
            self.redirect("/")

        if job.job_status < 4 :
            old_priority = job.settings.priority
            other_jobs = self.db.query(RenderJob).join(RenderJob.settings).filter( and_(RenderJob.job_status < 4,
                                                                                        Settings.priority < old_priority) ).all()
            job.settings.priority = 0
            for other_job in other_jobs:
                other_job.settings.priority += 1
            self.db.commit()
            
        self.redirect("/")
        
class Counter(dict):
    def __missing__(self, key):
        return 0
        
class ViewJobHandler( BaseHandler ):
    @tornado.web.asynchronous
    def get(self):
        job = self.getJobOrError()
        categories = self.generate_completed_archives( job )
        params = {
            "uri":self.request.uri,
            "job_data":job,
            "categories":categories,
            }
        for frame in params["job_data"].frames:
            if frame.start != None and frame.end != None:
                frame.time = frame.end-frame.start
            else:
                frame.time = ""
                
        self.render( "static/pages/view_job.html", **params )

    def generate_completed_archives(self, job ):
        if job.job_status != 2:
            return []
        
        images = self.db.query(Image)    
        images = images.join( Image.frame )
        images = images.join( Frame.job )
        images = images.filter( Frame.job_id == job.id )
        images = images.all()

        categories = Counter()
        for image in images:
            categories[image.category] += 1
        
        full_categories = []
        for category, value in categories.items():
            if value == len(job.frames):
                full_categories.append( category )
                
        return full_categories
        
        

class JobCancelHandler( BaseHandler ):
    @tornado.web.asynchronous
    def get(self):
        job_uuid = self.get_query_argument('uuid',default=None)
        self.manager.cancel_job(job_uuid);
        self.redirect( "/" )
        

class DownloadZipHandler( BaseHandler ):
    @tornado.gen.coroutine
    def get(self):
        job = self.getJobOrError()
        category = self.get_query_argument('category',default="render")
        if job==None:
            self.finish()
            return        
        save_dir = os.path.join(self.data_path,"completed_renders",job.name+"_"+job.uuid )
        zip_file = os.path.join( save_dir, job.uuid+"."+category+"."+"zip" )
        if not os.path.exists( zip_file ):        
            f=open( zip_file, 'wb' )
            zf = ZipFile(f, "w", allowZip64=True)
            images = self.db.query(Image)    
            images = images.join( Image.frame )
            images = images.join( Frame.job )
            images = images.filter( Frame.job_id == job.id )
            images = images.filter( Image.category == category )
            images = images.order_by(Frame.frame)
            images = images.all()
            if len(images) != (job.frame_end - job.frame_start + 1):
                zf.close()
                f.close()
                os.remove( zip_file )
                self.send_error(403);
                self.finish()
                return;
            for image in images:
                zf.write(image.path,arcname="render_{:08d}.{:s}".format(image.frame.frame, image.extension))
            zf.close()
            f.close()
            
        f=open( zip_file, 'rb' )
        self.set_header('Content-Type', 'application/zip')
        self.set_header("Content-Disposition", "attachment; filename=%s.zip" % (job.name+"_"+category) )
        while True:
            buf = f.read(64000)
            self.write( buf )
            yield self.flush()
            if len(buf) < 64000:
                break;            
        f.close()
        self.finish()
        
class JobSubmissionHandler( BaseHandler ):
    @tornado.web.asynchronous
    def get(self):
        job_types = self.db.query(SceneTypes).all()
        
        params = {
            "uri":self.request.uri,
            "missing":{},
            "values":{},
            "job_types":job_types,
        }
        params["new_job"] = False;
            
        self.render( "static/pages/jobsubmit.html", **params )
        
    def post(self):
        job_types = self.db.query(SceneTypes).all()
        submitter = self.get_argument("submitter", default=None, strip=True)
        email = self.get_argument("email", default=None, strip=True)
        name = self.get_argument("name", default=None, strip=True)
        scene = self.get_argument("scene", default=None, strip=True)
        frame_start = self.get_argument("frame-start", default=None, strip=True)
        frame_end = self.get_argument("frame-end", default=None, strip=True)
        job_type = self.get_argument("job-type", default=None, strip=True)
        print self.request.arguments
        
        params = {
            "uri":self.request.uri,
            "missing":{},
            "job_types":job_types,
        }
        
        if submitter == None or email == None or name == '' or scene == None or frame_start == None or frame_end == None or job_type == None:      
            params["new_job"] = False;
            params["missing"] = {"submitter":submitter == '',
                                 "email":email == '',
                                 "name":name == '',
                                 "scene":scene == '',
                                 "frame-start":frame_start == '',
                                 "frame-end":frame_end == '',
                                 "job-type":job_type == '',
                             }
            params["values"] = {"submitter":submitter,
                                "email":email,
                                "name":name,
                                "scene":scene,
                                "frame-start":frame_start,
                                "frame-end":frame_end,
                                "job-type":job_type,
                            }
        else:
            params["new_job"] = True;

            status = self.manager.validate_job(submitter,
                                               email,
                                               name,
                                               scene,
                                               int(frame_start),
                                               int(frame_end),
                                               job_type,
                                           )
            if status.good:
                jobType = self.db.query(SceneTypes).filter(SceneTypes.type_id==job_type).one()
                print jobType
                renderjob = RenderJob(submitter=submitter,
                                      email=email,
                                      name=name,
                                      scene=scene,
                                      frame_start=int(frame_start),
                                      frame_end=int(frame_end),
                                      type=jobType,
                )
                print renderjob

                results = self.manager.submit_job(renderjob)
                if results:
                    params["job_submit_success"] = True
                else:
                    params["job_submit_success"]  = False
                    params["job_submit_error"] = "Failed to commit to database."
            else:
                params["job_submit_success"] = False
                params["job_submit_error"] = status.err_msg
            
        self.render( "static/pages/jobsubmit.html", **params )           

class JobListing( BaseHandler ):
    @tornado.web.asynchronous
    def get(self):
        active_jobs = self.db.query(RenderJob).join(RenderJob.settings).filter(RenderJob.job_status==1).order_by(Settings.priority).all()
        jobs = []
        for job in active_jobs:
            jobs.append( [ job.uuid, job.type.type_id ] )
        self.write( json.dumps( jobs ) )
        self.finish()

def Rebuild_Previews(image, b ):
    wandImage = WandImage( file=b )
    wandImage.format = 'png'
    wandImage.sample(max(100,wandImage.width/4), max(wandImage.height/4,100))
    dirname = os.path.dirname( image.path )
    basename = os.path.basename( image.path )
    prefix, extension = os.path.splitext( basename )
    wandImage.save( filename=os.path.join( dirname, "preview_{:s}.png".format(prefix) ) )
    image.preview_path = os.path.join( dirname, "preview_{:s}.png".format(prefix) )

def Rebuild_PreviewAnimation( db, job, data_path ):
    images = db.query(Image)    
    images = images.join( Image.frame )
    images = images.join( Frame.job )
    images = images.filter( Frame.job_id == job.id )
    images = images.filter( Image.category == "render" )
    images = images.all()

    with WandImage() as gif_preview:
        for image in images:
            if image.preview_path:
                with WandImage( filename=image.preview_path ) as f:
                    gif_preview.sequence.append(f)
        for cursor in range( len( gif_preview.sequence )):
            with gif_preview.sequence[cursor] as f:
                f.delay = 4 # 4 hundredths of a second ~24 fps
        gif_preview.type='truecolor'
        save_path = os.path.join(data_path, "completed_renders", job.name+"_"+job.uuid )
        gif_preview.save( filename=os.path.join( save_path, "preview.gif" ) )
        
class StoreHandler( BaseHandler ):

    @tornado.web.asynchronous
    def post(self):
        frame = self.getFrameOrError()
        if frame==None:
            self.finish()
            return

        print "Recieved image store request with the following images: "
        print self.request.files.keys()

        for category in self.request.files.keys():
            render = self.request.files[category][0]
            original_fname = render['filename']
            extension = os.path.splitext(original_fname)[1][1:]
            fname = frame.uuid
            if category == "render":
                final_filename= ("{:08d}.".format(frame.frame))+extension
            else:
                final_filename= ("{:08d}.{:s}.".format(frame.frame, category))+extension
                
            save_path = os.path.join(self.data_path, "completed_renders", frame.job.name+"_"+frame.job.uuid )
            try:
                os.makedirs(save_path)
            except Exception, e:
                LOGGER.error("Error while creating save directory: {:s}".format(str(e)) )

            existing_images = self.db.query(Image).join(Image.frame).filter( and_(Image.category==category,
                                                                                 Image.frame==frame)).all()
            if len(existing_images) == 1:
                LOGGER.warning( "Received image store request for already stored image: Job {:d} Frame {:d} Category (:s}. Overwriting...".format( frame.job.id, frame.frame, category ) )
            if len(existing_images) > 1:
                LOGGER.warning( "Too many images recorded for: Job {:d} Frame {:d} Category (:s}. Failing store request...".format( frame.job.id, frame.frame, category ) )
                self.finish()
                return

            output_file = open(os.path.join(save_path, final_filename), 'w')           
            output_file.write(render['body'])
            output_file.close()

            if len(existing_images) == 0:
                image = Image( frame_id = frame.id,
                               path = os.path.join( save_path, final_filename ),
                               category = category,
                               extension = extension,
                               timestamp = datetime.now(),
                               uploader = self.request.remote_ip )                
                self.db.add(image);
                self.db.commit()
                
                if category == "render":
                    b = io.BytesIO( render["body"] )
                    Rebuild_Previews( image, b )                    
                    self.db.commit();
            else:
                existing_images[0].path = save_path;
                existing_images[0].timestamp = datetime.now()
                existing_images[0].uploader = self.request.remote_ip
                existing_images[0].extension = extension
                self.db.commit()
                
                if category == "render":
                    b = io.BytesIO( render["body"] )
                    Rebuild_Previews( existing_images[0], b )
                    self.db.commit()

            if category == "render":
                Rebuild_PreviewAnimation(self.db, frame.job, self.data_path )
                
        self.finish()
      
        
class FrameSettings( BaseHandler ):
    @tornado.web.asynchronous
    def get(self):
        frame = self.getFrameOrError()
        if frame==None:
            self.finish()
            return

        renderjob = frame.job
        settings = renderjob.settings
        settings_data = { 'retries':settings.retries,
                          'timeout':settings.timeout }
        self.write( json.dumps( settings_data ) );            
        self.finish()
                              
            
# class ManualStore( BaseHandler ):
#     @tornado.web.asynchronous
#     def post(self):
#         job_uuid = self.get_argument('job_uuid',default=None)
#         frame = self.get_argument('frame',default=None)
#         if job_uuid == None or frame == None:
#             print job_uuid
#             print frame
#             LOGGER.error( "Bad input data" );
#             self.send_error(500)
#             return
#         try:
#             frame_record = self.db.query(Frame).join(RenderJob).filter(RenderJob.uuid==job_uuid).filter(Frame.frame==frame).one();
#         except NoResultFound:
#             LOGGER.error( "No frame " + str(frame) + " found for job " + str(job_uuid) );
#             self.send_error(500);
#             return
#         else:           
#             render = self.request.files['frame_file'][0]
#             original_fname = render['filename']
#             extension = os.path.splitext(original_fname)[1]
#             fname = frame_record.uuid
#             final_filename= fname
#             try:
#                 os.mkdir(os.path.join(self.data_path,"frame_cache"))
#             except:
#                 pass
#             output_file = open(os.path.join(self.data_path,"frame_cache",final_filename), 'w')
#             output_file.write(render['body'])
#             output_file.close()
#             frame_record.status = 3
#             frame_record.end = datetime.now()
#             self.db.commit()   

#         self.finish()

class ToggleAutoRequeue( BaseHandler ):
    @tornado.web.asynchronous
    def get(self):
        job = self.getJobOrError()
        if job==None:
            self.finish()
            return

        if job.try_hard == 1:
            job.try_hard = 0
        else:
            job.try_hard = 1;

        self.db.commit()   

        self.finish();
    

class ManualRequeue( BaseHandler ):
    @tornado.web.asynchronous
    def get(self):        
        render_uuid = self.get_query_argument('uuid',default=None)
        if render_uuid == None:
            self.send_error(500)
            return
        try:
            frame = self.db.query(Frame).filter_by(uuid=render_uuid).one()
        except NoResultFound:
            self.send_error(500);
            return
        else:
            self.manager.requeue( frame )
            self.finish()

class FetchHandler( BaseHandler ):
    @tornado.gen.coroutine
    def get(self):
        frame = self.getFrameOrError();
        if frame==None:
            self.finish()
            return
        category = self.get_query_argument('category',default="render")
        image = self.db.query(Image).join(Image.frame).filter(and_(Image.category==category,
                                                                   Image.frame==frame)).one_or_none()
        if image != None:
            if not image.preview_path:
                Rebuild_Previews( image, open( image.path, 'rb' ) )
                self.db.commit();

            psize = 0;
            with open(image.preview_path, 'rb' ) as preview:
                self.set_header('Content-type', 'image/png')
                while True:
                    buf = preview.read(64000)
                    self.write( buf )
                    yield self.flush()
                    psize+=len(buf)
                    if len(buf) < 64000:
                        break;            
            self.set_header('Content-length', psize )
            self.finish()
        else:
            self.send_error(403)
            self.finish()


class FetchPreview( BaseHandler ):
    @tornado.gen.coroutine
    def get(self):
        job = self.getJobOrError();
        if job==None:
            self.finish()
            return
        psize = 0;

        preview_path = os.path.join( self.data_path, "completed_renders", job.name + "_" + job.uuid, "preview.gif" )        
        try:
            with open(preview_path, 'rb' ) as preview:
                self.set_header('Content-type', 'image/gif')
                while True:
                    buf = preview.read(64000)
                    self.write( buf )
                    yield self.flush()
                    psize+=len(buf)
                    if len(buf) < 64000:
                        break;            
                self.set_header('Content-length', psize )
                self.finish()
        except:
            self.send_error(403)
            self.finish()
            
            
def valid_path(path):
    if os.path.isdir( path ):
        return path
    else:
        msg = '"%s" is not a valid path name.' % path 
        raise argparse.ArgumentTypeError(msg)
    



parser = argparse.ArgumentParser(description='Plumage Manager')
parser.add_argument('-p','--port', default=8888, type=int )
parser.add_argument('-d','--data_path', default='/var/plumage/data', type=valid_path )
parser.add_argument('-s','--scene_path', default='/var/plumage/scenes', type=valid_path )
parser.add_argument('-a','--ampq_server', default='localhost:5672', type=str )
parser.add_argument('-U','--ampq_user', default='guest', type=str )
parser.add_argument('-P','--ampq_password', default='guest', type=str )

def main():
    args = parser.parse_args()
    manager_config = {
        "amqp_url": 'http://%s:%s@%s'%(args.ampq_user,args.ampq_password,args.ampq_server),
        "kill_jobs_on_start": False,
        "data_path": args.data_path,
        "scene_path": args.scene_path,
    }
    logging.basicConfig(level=logging.INFO)

    engine = create_engine('sqlite:///'+os.path.abspath(os.path.join(args.data_path,'local.db')))
    create_all(engine)
    app = Application(engine=engine,data_path=args.data_path, scene_path=args.scene_path)
    io_loop = tornado.ioloop.IOLoop.instance()
    server_manager = ServerManager( io_loop, app.db, **manager_config );
    app.server_manager = server_manager
    app.server_manager.connect()       
    app.listen(args.port, '0.0.0.0', max_buffer_size=500*1024*1024)
    io_loop.start()

if __name__ == '__main__':
    
    main()
