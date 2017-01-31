import os
import sys
from sqlalchemy import Column, ForeignKey, Integer, String, DateTime, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, backref
from sqlalchemy import create_engine

Base = declarative_base()

class ManagerSettings(Base):
    __tablename__ = 'managersettings'
    id = Column(Integer, primary_key=True)
    broker = Column(Text)
    broker_manager_user = Column(String(128))
    broker_manager_pass = Column(String(128))
    sceneData_path = Column(Text)
    managerData_path = Column(Text)
    timeout = Column(Integer)
    retries = Column(Integer)

    
class Settings(Base):
    __tablename__ = 'settings'
    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey('renderjob.id'), nullable=False)
    timeout = Column(Integer)
    retries = Column(Integer)
    priority = Column(Integer, default=0)
    try_hard = Column(Integer, default=0)

class RenderJob(Base):
    __tablename__ = 'renderjob'
    id = Column(Integer, primary_key=True, autoincrement=True)
    uuid = Column(String(34))
    name = Column(String(250), nullable=False)
    submitter = Column(String(250), nullable=False)
    email = Column(String(250), nullable=False)
    scene = Column(String(250), nullable=False)
    start = Column(DateTime)
    end = Column(DateTime)
    frame_start = Column(Integer)
    frame_end = Column(Integer)
    frames = relationship("Frame", order_by="Frame.id", backref="job", cascade="save-update, merge, delete, delete-orphan" )
    job_status = Column(Integer)
    eta = Column(DateTime)
    settings = relationship("Settings", uselist=False, backref="job", cascade="save-update, merge, delete, delete-orphan")
    type_id = Column("type", Integer, ForeignKey('scenetypes.id'), default=0)
    type = relationship("SceneTypes")

class SceneTypes(Base):
    __tablename__ = 'scenetypes'
    id = Column(Integer, primary_key=True)
    type_id = Column(Text, nullable=False)
    type_HR = Column(Text)
    signatureFile = Column(Text)
    
class Frame(Base):
    __tablename__ = 'frame'
    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey('renderjob.id'), nullable=False)
    frame = Column(Integer)
    status = Column(Integer)
    uuid = Column(String(34))
    start = Column(DateTime)
    end = Column(DateTime)
    node = Column(String(256),default="")
    frame_metadata = Column(Text,default="{}")    
    fail_count = Column(Integer,default=0)
    images = relationship("Image", order_by="Image.id", backref="frame", cascade="save-update, merge, delete, delete-orphan")
    
class Image(Base):
    __tablename__ = 'image'
    id = Column(Integer, primary_key=True)
    frame_id = Column(Integer, ForeignKey('frame.id'), nullable=False)
    path = Column(String(250), nullable=False)
    category = Column(String(250), nullable=True)
    timestamp = Column(DateTime)
    uploader = Column(String(250), nullable=False)
    extension = Column(String(20), nullable=False)
    preview_path = Column(String(250))
    
metadata = Base.metadata
    
def create_all(engine):
    metadata.create_all(engine)
