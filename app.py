import base64

# ... всередині циклу for i, scene in enumerate(scenes):
img_path = f"{work_dir}/img_{i}.jpg"
audio_path = f"{work_dir}/audio_{i}.mp3"

img_data = requests.get(scene['image_url']).content
with open(img_path, 'wb') as f:
    f.write(img_data)

audio_data = base64.b64decode(scene['audio_base64'])
with open(audio_path, 'wb') as f:
    f.write(audio_data)
