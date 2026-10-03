import os

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'campus-sync-secret-key-2024'
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or 'sqlite:///campus_sync.db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
