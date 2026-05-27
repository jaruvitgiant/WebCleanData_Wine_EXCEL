from django.urls import path
from . import views

urlpatterns = [
    path('', views.upload_file, name='upload_file'),
    path('preview/', views.preview_file, name='preview_file'),
    path('export/pdf/', views.export_pdf, name='export_pdf'),
]