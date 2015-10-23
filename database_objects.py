import os
import sys
from sqlalchemy import Column, ForeignKey, Integer, String, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, backref
from sqlalchemy import create_engine

Base = declarative_base()

class RenderJob(Base):
    __tablename__ = 'renderjob'
    id = Column(Integer, primary_key=True)
    uuid = Column(String(34))
    name = Column(String(250), nullable=False)
    submitter = Column(String(250), nullable=False)
    email = Column(String(250), nullable=False)
    scene = Column(String(250), nullable=False)
    frame_start = Column(Integer)
    frame_end = Column(Integer)
    frames = relationship("Frame", order_by="Frame.id", backref="job")
    job_status = Column(Integer)
    try_hard = Column(Integer, default=0)
    eta = Column(DateTime)
    

class Frame(Base):
    __tablename__ = 'frame'
    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey('renderjob.id'))
    frame = Column(Integer)
    status = Column(Integer)
    uuid = Column(String(34))
    start = Column(DateTime)
    end = Column(DateTime)
    
metadata = Base.metadata
    
def create_all(engine):
    metadata.create_all(engine)
