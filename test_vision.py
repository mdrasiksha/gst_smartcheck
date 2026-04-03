# test_vision.py
from google.cloud import vision

client = vision.ImageAnnotatorClient()
print("✅ Vision API working")