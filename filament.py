"""
Mask R-CNN
Train on the toy filament dataset and implement color splash effect.

Copyright (c) 2018 Matterport, Inc.
Licensed under the MIT License (see LICENSE for details)
Written by Waleed Abdulla

------------------------------------------------------------

Usage: import the module (see Jupyter notebooks for examples), or run from
       the command line as such:

    # Train a new model starting from pre-trained COCO weights
    python3 filament.py train --dataset=/path/to/filament/dataset --weights=coco

    # Resume training a model that you had trained earlier
    python3 filament.py train --dataset=/path/to/filament/dataset --weights=last

    # Train a new model starting from ImageNet weights
    python3 filament.py train --dataset=/path/to/filament/dataset --weights=imagenet

    # Apply color splash to an image
    python3 filament.py splash --weights=/path/to/weights/file.h5 --image=<URL or path to file>

    # Apply color splash to video using the last weights you trained
    python3 filament.py splash --weights=last --video=<URL or path to file>
"""
import cv2
import os
import glob
import sys
import json
import datetime
import numpy as np
import skimage.draw
import skimage.io
from pylab import array, plot, show, axis, arange, figure, uint8 
from imgaug import augmenters as iaa
from mrcnn import visualize
from mrcnn.visualize import display_images
import matplotlib.pyplot as plt
from scipy import signal
# Root directory of the project
ROOT_DIR = os.path.abspath(".")

# Import Mask RCNN
sys.path.append(ROOT_DIR)  # To find local version of the library
from mrcnn.config import Config
from mrcnn import model as modellib, utils

# Path to trained weights file
COCO_WEIGHTS_PATH = os.path.join(ROOT_DIR, "mask_rcnn_coco.h5")

# Directory to save logs and model checkpoints, if not provided
# through the command line argument --logs
DEFAULT_LOGS_DIR = os.path.join(ROOT_DIR, "logs")

############################################################
#  Configurations
############################################################


class filamentConfig(Config):
    """Configuration for training on the toy  dataset.
    Derives from the base Config class and overrides some values.
    """
    # Give the configuration a recognizable name
    NAME = "filament"

    # We use a GPU with 12GB memory, which can fit two images.
    # Adjust down if you use a smaller GPU.
    IMAGES_PER_GPU = 2

    # Number of classes (including background)
    NUM_CLASSES = 1 + 4  # Background + filament

    # Number of training steps per epoch
    STEPS_PER_EPOCH = 100

    # Skip detections with < 90% confidence
    DETECTION_MIN_CONFIDENCE = 0.8

############################################################
#  Dataset
############################################################

class filamentDataset(utils.Dataset):

    def load_filament(self, dataset_dir, subset):
        """Load a subset of the filament dataset.
        dataset_dir: Root directory of the dataset.
        subset: Subset to load: train or val
        """
        # Add classes. We have only one class to add.
        self.add_class("filament", 1, "filament")

        # Train or validation dataset?
        assert subset in ["train", "val"]
        dataset_dir = os.path.join(dataset_dir, subset)

        # Load annotations
        # VGG Image Annotator (up to version 1.6) saves each image in the form:
        # { 'filename': '28503151_5b5b7ec140_b.jpg',
        #   'regions': {
        #       '0': {
        #           'region_attributes': {},
        #           'shape_attributes': {
        #               'all_points_x': [...],
        #               'all_points_y': [...],
        #               'name': 'polygon'}},
        #       ... more regions ...
        #   },
        #   'size': 100202
        # }
        # We mostly care about the x and y coordinates of each region
        # Note: In VIA 2.0, regions was changed from a dict to a list.
        annotations = json.load(open(os.path.join(dataset_dir, "via_region_data.json")))
        annotations = list(annotations.values())  # don't need the dict keys

        # The VIA tool saves images in the JSON even if they don't have any
        # annotations. Skip unannotated images.
        annotations = [a for a in annotations if a['regions']]

        # Add images
        for a in annotations:
            # Get the x, y coordinaets of points of the polygons that make up
            # the outline of each object instance. These are stores in the
            # shape_attributes (see json format above)
            # The if condition is needed to support VIA versions 1.x and 2.x.
            if type(a['regions']) is dict:
                polygons = [r['shape_attributes'] for r in a['regions'].values()]
            else:
                polygons = [r['shape_attributes'] for r in a['regions']] 

            # load_mask() needs the image size to convert polygons to masks.
            # Unfortunately, VIA doesn't include it in JSON, so we must read
            # the image. This is only managable since the dataset is tiny.
            image_path = os.path.join(dataset_dir, a['filename'])
            image = skimage.io.imread(image_path)
            height, width = image.shape[:2]

            self.add_image(
                "filament",
                image_id=a['filename'],  # use file name as a unique image id
                path=image_path,
                width=width, height=height,
                polygons=polygons)

    def load_mask(self, image_id):
        """Generate instance masks for an image.
       Returns:
        masks: A bool array of shape [height, width, instance count] with
            one mask per instance.
        class_ids: a 1D array of class IDs of the instance masks.
        """
        # If not a filament dataset image, delegate to parent class.
        image_info = self.image_info[image_id]
        if image_info["source"] != "filament":
            return super(self.__class__, self).load_mask(image_id)

        # Convert polygons to a bitmap mask of shape
        # [height, width, instance_count]
        info = self.image_info[image_id]
        mask = np.zeros([info["height"], info["width"], len(info["polygons"])],
                        dtype=np.uint8)
        for i, p in enumerate(info["polygons"]):
            # Get indexes of pixels inside the polygon and set them to 1
            rr, cc = skimage.draw.polygon(p['all_points_y'], p['all_points_x'])
            mask[rr, cc, i] = 1

        # Return mask, and array of class IDs of each instance. Since we have
        # one class ID only, we return an array of 1s
        return mask.astype(np.bool), np.ones([mask.shape[-1]], dtype=np.int32)

    def image_reference(self, image_id):
        """Return the path of the image."""
        info = self.image_info[image_id]
        if info["source"] == "filament":
            return info["path"]
        else:
            super(self.__class__, self).image_reference(image_id)


def train(model):
    """Train the model."""
    # Training dataset.
    dataset_train = filamentDataset()
    dataset_train.load_filament(args.dataset, "train")
    dataset_train.prepare()

    # Validation dataset
    dataset_val = filamentDataset()
    dataset_val.load_filament(args.dataset, "val")
    dataset_val.prepare()
    # Image augmentation
    # http://imgaug.readthedocs.io/en/latest/source/augmenters.html
    augmentation = iaa.SomeOf((0, 4), [
        iaa.Fliplr(0.5),
        iaa.Flipud(0.5),
        iaa.OneOf([iaa.Affine(rotate=90),
                   iaa.Affine(rotate=45),
                   iaa.Affine(rotate=10),
                   iaa.Affine(rotate=5)]),
        iaa.Multiply((0.6, 0.2)),
        iaa.GaussianBlur(sigma=(1.0, 3.0)),
    ])
    # *** This training schedule is an example. Update to your needs ***
    # Since we're using a very small dataset, and starting from
    # COCO trained weights, we don't need to train too long. Also,
    # no need to train all layers, just the heads should do it.
    
#    print("Training network heads")
#    model.train(dataset_train, dataset_val,
#                learning_rate=0.01,
#                epochs=20,
#                augmentation=augmentation,
#                layers='heads')
#    print("Train all layers at 0.001")
#    model.train(dataset_train, dataset_val,
#                learning_rate=0.001,
#                epochs=100,
#                augmentation=augmentation,
#                layers='all')
#    print("Train all layers at 0.001")
#    model.train(dataset_train, dataset_val,
#                learning_rate=0.001,
#                epochs=100,
                #augmentation=augmentation,
#                layers='all')
#    print("Train all layers at 0.0005")
    model.train(dataset_train, dataset_val,
                learning_rate=0.001,
                epochs=100,
               # augmentation=augmentation,
                layers='all')

#    model.train(dataset_train, dataset_val,
#                learning_rate=config.LEARNING_RATE,
#                epochs=100,
#                layers='all')

'''def color_splash(image, mask):
    """Apply color splash effect.
    image: RGB image [height, width, 3]
    mask: instance segmentation mask [height, width, instance count]

    Returns result image.
    """
    # Make a grayscale copy of the image. The grayscale copy still
    # has 3 RGB channels, though.
    gray = skimage.color.gray2rgb(skimage.color.rgb2gray(image)) * 255
    # Copy color pixels from the original color image where mask is set
    if mask.shape[-1] > 0:
        # We're treating all instances as one, so collapse the mask into one layer
        mask = (np.sum(mask, -1, keepdims=True) >= 1)
        splash = np.where(mask, image, gray).astype(np.uint8)
    else:
        splash = gray.astype(np.uint8)
    return splash


def detect_and_color_splash(model, image_path=None, video_path=None):
    assert image_path or video_path

    # Image or video?
    if image_path:
        # Run model detection and generate the color splash effect
        print("Running on {}".format(args.image))
        # Read image
        image = skimage.io.imread(args.image)
        # Detect objects
        r = model.detect([image], verbose=1)[0]
        # Color splash
        splash = color_splash(image, r['masks'])
        # Save output
        file_name = "splash_{:%Y%m%dT%H%M%S}.png".format(datetime.datetime.now())
        skimage.io.imsave(file_name, splash)
    elif video_path:
        import cv2
        # Video capture
        vcapture = cv2.VideoCapture(video_path)
        width = int(vcapture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(vcapture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = vcapture.get(cv2.CAP_PROP_FPS)

        # Define codec and create video writer
        file_name = "splash_{:%Y%m%dT%H%M%S}.avi".format(datetime.datetime.now())
        vwriter = cv2.VideoWriter(file_name,
                                  cv2.VideoWriter_fourcc(*'MJPG'),
                                  fps, (width, height))

        count = 0
        success = True
        while success:
            print("frame: ", count)
            # Read next image
            success, image = vcapture.read()
            if success:
                # OpenCV returns images as BGR, convert to RGB
                image = image[..., ::-1]
                # Detect objects
                r = model.detect([image], verbose=0)[0]
                # Color splash
                splash = color_splash(image, r['masks'])
                # RGB -> BGR to save image to video
                splash = splash[..., ::-1]
                # Add image to video writer
                vwriter.write(splash)
                count += 1
        vwriter.release()
    print("Saved to ", file_name)
'''

def threshold(image):
    def thresh(a, b, max_value, C):
        return max_value if a > b - C else 0

    def mask(a,b):
        return a if b > 100 else 0

    def unmask(a,b,c):
        return b if c > 100 else a

    v_unmask = np.vectorize(unmask)
    v_mask = np.vectorize(mask)
    v_thresh = np.vectorize(thresh)

    def block_size(size):
        block = np.ones((size, size), dtype='d')
        block[int((size-1)/2), int((size-1)/2)] = 0
        return block

    def get_number_neighbours(mask,block):
        '''returns number of unmasked neighbours of every element within block'''
        mask = mask / 255.0
        return signal.convolve2d(mask, block, mode='same', boundary='symm')

    def masked_adaptive_threshold(image,mask,max_value,size,C):
        '''thresholds only using the unmasked elements'''
        block = block_size(size)
        conv = signal.convolve2d(image, block, mode='same', boundary='symm')
        mean_conv = conv / get_number_neighbours(mask,block)
        return v_thresh(image, mean_conv, max_value,C)

    mask = cv2.adaptiveThreshold(image, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 69, 4)
    mask = cv2.bitwise_not(mask)
    original_image = np.asarray(image)
    mask = np.asarray(mask)
    image = v_mask(original_image, mask)
    image = masked_adaptive_threshold(image,mask,max_value=255,size=9,C=4)
    image = v_unmask(original_image, image, mask)
    image = image.astype(np.uint8)
    return image


def segment_filament(mask, image, image_path=None):
    if image_path:
    	if mask.shape[-1] > 0:
    		mask = (np.sum(mask, -1, keepdims=True) >= 1) 		
    		binary = np.zeros_like(image, dtype=np.uint8)
    		binary.fill(255)
    		binary = np.where(mask, image, binary)
    		binary = cv2.cvtColor(binary, cv2.COLOR_BGR2GRAY)
    		filament = threshold(binary)
    		cv2.imwrite(image_path,filament)
        
def batch_detect(model, dir_path=None):
    files = glob.glob(dir_path+"*.png")
    for x in files:
    	detect_filament(model, x)
    
def detect_filament(model, image_path=None):
    assert image_path

    if image_path:
#    	for i in range(0,2):
	        file_ext = str(image_path).split('/')
	        filename = file_ext[-1].split('.')    
	        
	        # Run model detection
	        log_filename = "results/"+filename[0]+"_{:%Y%m%d_%H_%M_%S}.txt".format(datetime.datetime.now())
	        log_file = open(log_filename, "a")
        	log_image_name = "File Name:  {}".format(file_ext[-1])
        	print(log_image_name, file=log_file)
#        	with open(log_file, "a") as myfile:
#        		myfile.write(log_filename)
        	start_time = datetime.datetime.now()
        	# Read image
        	image = skimage.io.imread(image_path)
        	# Detect objects
        	r = model.detect([image], verbose=1)[0]
        	end_time = datetime.datetime.now()
        	duration = end_time-start_time
        	mask = r['masks']
		# Compute Bounding box
        	bbox = utils.extract_bboxes(mask)
        	# Color splash
        	rect = r['rois']
        	print("Detection Duration: "+ str(duration), file=log_file)       
        	print("Bounding Boxes", file=log_file)
        	print(rect, file=log_file)
        	file_name_binary = "results/"+filename[0]+"_{:%Y%m%d_%H_%M_%S}_binary.png".format(start_time)
        	start_time = datetime.datetime.now()
        	segment_filament(mask, image, file_name_binary) 
        	end_time = datetime.datetime.now()
        	duration = end_time-start_time
        	print("Segmentation Duration: "+ str(duration), file=log_file)       	
        	log_file.close()
#        cv2.imshow('med',image)
#        closeWindow = -1
#        while closeWindow<0:
#            closeWindow = cv2.waitKey(1) 
#        cv2.destroyAllWindows()

#save boundary
        	file_name = "results/"+filename[0]+"_{:%Y%m%d_%H_%M_%S}_border.png".format(start_time)
        	visualize.display_instances(image, r['rois'], r['masks'], r['class_ids'], r['scores'],  title="Boundary Prediction",show_mask=False, show_bbox=False, captions=False, show_border="black", show_label=False, save_image=file_name)

#save mask                              
        	file_name_mask = "results/"+filename[0]+"_{:%Y%m%d_%H_%M_%S}_mask.png".format(start_time)
        	visualize.display_instances(image, r['rois'], r['masks'], r['class_ids'], r['scores'], title="Mask Prediction",show_mask=True, show_bbox=False, captions=False, show_border=False, show_label=False, save_image=file_name_mask)

#save bounding box                              
        	file_name_box = "results/"+filename[0]+"_{:%Y%m%d_%H_%M_%S}_bbox.png".format(start_time)
        	visualize.display_instances(image, r['rois'], r['masks'], r['class_ids'], r['scores'], title="Bounding Box Prediction",show_mask=False, show_bbox=False, colors=None, captions=None, show_border=True, show_label=False)
        	for i in range(0,len(rect)):
	        	cv2.rectangle(image,((rect[i][1]-10),(rect[i][0]-10)),((rect[i][3]+10),(rect[i][2]+10)),(255,255,255),2)
        	cv2.imwrite(file_name_box, image)

#        file_name2 = "results/detect_{:%Y%m%dT%H%M%S}_mask.png".format(datetime.datetime.now())
#        cv2.imwrite(file_name2, image)
#        for i in range(0,len(rect)):
#	        cv2.rectangle(image,((rect[i][1]-10),(rect[i][0]-10)),((rect[i][3]+10),(rect[i][2]+10)),(255,255,255),1)
#        skimage.io.imsave(file_name, image)
#    print("Saved to ", file_name)


############################################################
#  Training
############################################################

if __name__ == '__main__':
    import argparse

    # Parse command line arguments
    parser = argparse.ArgumentParser(
        description='Train Mask R-CNN to detect filaments.')
    parser.add_argument("command",
                        metavar="<command>",
                        help="'train' or 'splash'")
    parser.add_argument('--dataset', required=False,
                        metavar="/path/to/filament/dataset/",
                        help='Directory of the filament dataset')
    parser.add_argument('--weights', required=True,
                        metavar="/path/to/weights.h5",
                        help="Path to weights .h5 file or 'coco'")
    parser.add_argument('--logs', required=False,
                        default=DEFAULT_LOGS_DIR,
                        metavar="/path/to/logs/",
                        help='Logs and checkpoints directory (default=logs/)')
    parser.add_argument('--image', required=False,
                        metavar="path or URL to image",
                        help='Image to apply the detection')
    parser.add_argument('--video', required=False,
                        metavar="path or URL to video",
                        help='Image to apply the detection')
    parser.add_argument('--dir', required=False,
                        metavar="path to batch image detection",
                        help='Image to apply the detection')                        
    args = parser.parse_args()

    # Validate arguments
    if args.command == "train":
        assert args.dataset, "Argument --dataset is required for training"
###    elif args.command == "splash":
###        assert args.image or args.video,\
###               "Provide --image or --video to apply color splash"
    elif args.command == "detect":
        assert args.image,\
               "Provide --image to detect filament"

    print("Weights: ", args.weights)
    print("Dataset: ", args.dataset)
    print("Logs: ", args.logs)

    # Configurations
    if args.command == "train":
        config = filamentConfig()
    else:
        class InferenceConfig(filamentConfig):
            # Set batch size to 1 since we'll be running inference on
            # one image at a time. Batch size = GPU_COUNT * IMAGES_PER_GPU
            GPU_COUNT = 1
            IMAGES_PER_GPU = 1
        config = InferenceConfig()
    config.display()

    # Create model
    if args.command == "train":
        model = modellib.MaskRCNN(mode="training", config=config,
                                  model_dir=args.logs)
    else:
        model = modellib.MaskRCNN(mode="inference", config=config,
                                  model_dir=args.logs)

    # Select weights file to load
    if args.weights.lower() == "coco":
        weights_path = COCO_WEIGHTS_PATH
        # Download weights file
        if not os.path.exists(weights_path):
            utils.download_trained_weights(weights_path)
    elif args.weights.lower() == "last":
        # Find last trained weights
        weights_path = model.find_last()
    elif args.weights.lower() == "imagenet":
        # Start from ImageNet trained weights
        weights_path = model.get_imagenet_weights()
    else:
        weights_path = args.weights

    # Load weights
    print("Loading weights ", weights_path)
    if args.weights.lower() == "coco":
        # Exclude the last layers because they require a matching
        # number of classes
        model.load_weights(weights_path, by_name=True, exclude=[
            "mrcnn_class_logits", "mrcnn_bbox_fc",
            "mrcnn_bbox", "mrcnn_mask"])
    else:
        model.load_weights(weights_path, by_name=True)

    # Train or evaluate
    if args.command == "train":
        train(model)
###    elif args.command == "splash":
###        detect_and_color_splash(model, image_path=args.image,
###                                video_path=args.video)
    elif args.command == "detect":
        detect_filament(model, image_path=args.image)                                
    elif args.command == "batch":
        batch_detect(model, dir_path=args.dir)           
    else:
        print("'{}' is not recognized. "
              "Use 'train' or 'splash'".format(args.command))
