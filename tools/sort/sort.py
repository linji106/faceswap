#!/usr/bin/env python3
"""
A tool that allows for sorting and grouping images in different ways.
"""
import logging
import os
import sys
import operator
from concurrent import futures
from shutil import copyfile

import numpy as np
import cv2
from tqdm import tqdm

# faceswap imports
from lib.serializer import get_serializer_from_filename
from lib.align import AlignedFace, DetectedFace
from lib.image import FacesLoader, read_image, read_image_meta_batch
from lib.utils import FaceswapError
from plugins.extract.recognition.vgg_face2_keras import VGGFace2 as VGGFace
from plugins.extract.pipeline import Extractor, ExtractMedia

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class Sort():
    """ Sorts folders of faces based on input criteria """
    # pylint: disable=no-member

    def __init__(self, arguments):
        self._args = arguments
        self.changes = None
        self.serializer = None
        self._vgg_face = None
        self._loader = FacesLoader(self._args.input_dir)

    def process(self):
        """ Main processing function of the sort tool """

        # Setting default argument values that cannot be set by argparse

        # Set output folder to the same value as input folder
        # if the user didn't specify it.
        if self._args.output_dir is None:
            logger.verbose("No output directory provided. Using input folder as output folder.")
            self._args.output_dir = self._args.input_dir

        # Assigning default threshold values based on grouping method
        if (self._args.final_process == "folders"
                and self._args.min_threshold < 0.0):
            method = self._args.group_method.lower()
            if method == 'face-cnn':
                self._args.min_threshold = 7.2
            elif method == 'hist':
                self._args.min_threshold = 0.3

        # Load VGG Face if sorting by face
        if self._args.sort_method.lower() == "face":
            self._vgg_face = VGGFace(exclude_gpus=self._args.exclude_gpus)
            self._vgg_face.init_model()

        # If logging is enabled, prepare container
        if self._args.log_changes:
            self.changes = {}

            # Assign default sort_log.json value if user didn't specify one
            if self._args.log_file_path == 'sort_log.json':
                self._args.log_file_path = os.path.join(self._args.input_dir,
                                                        'sort_log.json')

            # Set serializer based on log file extension
            self.serializer = get_serializer_from_filename(self._args.log_file_path)

        # Prepare sort, group and final process method names
        _sort = "sort_" + self._args.sort_method.lower()
        _group = "group_" + self._args.group_method.lower()
        _final = "final_process_" + self._args.final_process.lower()
        if _sort.startswith('sort_color-'):
            self._args.color_method = _sort.replace('sort_color-', '')
            _sort = _sort[:10]
        self._args.sort_method = _sort.replace('-', '_')
        self._args.group_method = _group.replace('-', '_')
        self._args.final_process = _final.replace('-', '_')

        self.sort_process()

    def launch_aligner(self):
        """ Load the aligner plugin to retrieve landmarks """
        extractor = Extractor(None, "fan", None,
                              normalize_method="hist", exclude_gpus=self._args.exclude_gpus)
        extractor.set_batchsize("align", 1)
        extractor.launch()
        return extractor

    @staticmethod
    def alignment_dict(filename, image):
        """ Set the image to an ExtractMedia object for alignment """
        height, width = image.shape[:2]
        face = DetectedFace(left=0, width=width, top=0, height=height)
        return ExtractMedia(filename, image, detected_faces=[face])

    def _get_landmarks(self):
        """ Multi-threaded, parallel and sequentially ordered landmark loader """
        extractor = self.launch_aligner()
        filename_list, image_list = self._get_images()
        feed_list = list(map(Sort.alignment_dict, filename_list, image_list))
        landmarks = np.zeros((len(feed_list), 68, 2), dtype='float32')

        logger.info("Finding landmarks in images...")
        # TODO thread the put to queue so we don't have to put and get at the same time
        # Or even better, set up a proper background loader from disk (i.e. use lib.image.ImageIO)
        for idx, feed in enumerate(tqdm(feed_list, desc="Aligning", file=sys.stdout)):
            extractor.input_queue.put(feed)
            landmarks[idx] = next(extractor.detected_faces()).detected_faces[0].landmarks_xy

        return filename_list, image_list, landmarks

    def _get_images(self):
        """ Multi-threaded, parallel and sequentially ordered image loader """
        logger.info("Loading images...")
        filename_list = self.find_images(self._args.input_dir)
        with futures.ThreadPoolExecutor() as executor:
            image_list = list(tqdm(executor.map(read_image, filename_list),
                                   desc="Loading Images",
                                   file=sys.stdout,
                                   total=len(filename_list)))

        return filename_list, image_list

    def sort_process(self):
        """
        This method dynamically assigns the functions that will be used to run
        the core process of sorting, optionally grouping, renaming/moving into
        folders. After the functions are assigned they are executed.
        """
        sort_method = self._args.sort_method.lower()
        group_method = self._args.group_method.lower()
        final_method = self._args.final_process.lower()

        img_list = getattr(self, sort_method)()
        if "folders" in final_method:
            # Check if non-dissimilarity sort method and group method are not the same
            if group_method.replace('group_', '') not in sort_method:
                img_list = self.reload_images(group_method, img_list)
                img_list = getattr(self, group_method)(img_list)
            else:
                img_list = getattr(self, group_method)(img_list)

        getattr(self, final_method)(img_list)

        logger.info("Done.")

    # Methods for sorting
    def sort_distance(self):
        """ Sort by comparison of face landmark points to mean face by average distance of core
        landmarks. """
        logger.info("Sorting by average distance of landmarks...")
        filenames = []
        distances = []
        filelist = [os.path.join(self._loader.location, fname)
                    for fname in os.listdir(self._loader.location)
                    if os.path.splitext(fname)[-1] == ".png"]
        for filename, metadata in tqdm(read_image_meta_batch(filelist),
                                       total=len(filelist),
                                       desc="Calculating Distances"):
            if not metadata:
                msg = ("The images to be sorted do not contain alignment data. Images must have "
                       "been generated by Faceswap's Extract process.\nIf you are sorting an "
                       "older faceset, then you should re-extract the faces from your source "
                       "alignments file to generate this data.")
                raise FaceswapError(msg)
            alignments = metadata["itxt"]["alignments"]
            aligned_face = AlignedFace(np.array(alignments["landmarks_xy"], dtype="float32"))
            filenames.append(filename)
            distances.append(aligned_face.average_distance)

        logger.info("Sorting...")
        matched_list = list(zip(filenames, distances))
        img_list = sorted(matched_list, key=operator.itemgetter(1))
        return img_list

    def sort_blur(self):
        """ Sort by blur amount """
        logger.info("Sorting by estimated image blur...")

        blurs = [(filename, self.estimate_blur(image, metadata))
                 for filename, image, metadata in tqdm(self._loader.load(),
                                                       desc="Estimating blur",
                                                       total=self._loader.count,
                                                       leave=False)]
        logger.info("Sorting...")
        return sorted(blurs, key=lambda x: x[1], reverse=True)

    def sort_blur_fft(self):
        """ Sort by fft filtered blur amount with fft"""
        logger.info("Sorting by estimated fft filtered image blur...")

        fft_blurs = [(filename, self.estimate_blur_fft(image, metadata))
                     for filename, image, metadata in tqdm(self._loader.load(),
                                                           desc="Estimating fft blur score",
                                                           total=self._loader.count,
                                                           leave=False)]
        logger.info("Sorting...")
        return sorted(fft_blurs, key=lambda x: x[1], reverse=True)

    def sort_color(self):
        """ Score by channel average intensity """
        logger.info("Sorting by channel average intensity...")
        desired_channel = {'gray': 0, 'luma': 0, 'orange': 1, 'green': 2}
        method = self._args.color_method
        channel_to_sort = next(v for (k, v) in desired_channel.items() if method.endswith(k))
        filename_list, image_list = self._get_images()

        logger.info("Converting to appropriate colorspace...")
        same_size = all(img.size == image_list[0].size for img in image_list)
        images = np.array(image_list, dtype='float32')[None, ...] if same_size else image_list
        converted_images = self._convert_color(images, same_size, method)

        logger.info("Scoring each image...")
        if same_size:
            scores = np.average(converted_images[0], axis=(1, 2))
        else:
            progress_bar = tqdm(converted_images, desc="Scoring", file=sys.stdout)
            scores = np.array([np.average(image, axis=(0, 1)) for image in progress_bar])

        logger.info("Sorting...")
        matched_list = list(zip(filename_list, scores[:, channel_to_sort]))
        sorted_file_img_list = sorted(matched_list, key=operator.itemgetter(1), reverse=True)
        return sorted_file_img_list

    def sort_face(self):
        """ Sort by identity similarity """
        logger.info("Sorting by identity similarity...")
        filenames = []
        preds = []
        for filename, image, metadata in tqdm(self._loader.load(),
                                              desc="Classifying Faces",
                                              total=self._loader.count,
                                              leave=False):
            if not metadata:
                msg = ("The images to be sorted do not contain alignment data. Images must have "
                       "been generated by Faceswap's Extract process.\nIf you are sorting an "
                       "older faceset, then you should re-extract the faces from your source "
                       "alignments file to generate this data.")
                raise FaceswapError(msg)
            alignments = metadata["alignments"]
            face = AlignedFace(np.array(alignments["landmarks_xy"], dtype="float32"),
                               image=image,
                               centering="legacy",
                               size=self._vgg_face.input_size,
                               is_aligned=True).face
            filenames.append(filename)
            preds.append(self._vgg_face.predict(face))

        logger.info("Sorting by ward linkage...")

        indices = self._vgg_face.sorted_similarity(np.array(preds), method="ward")
        img_list = np.array(filenames)[indices]
        return img_list

    def sort_face_cnn(self):
        """ Sort by landmark similarity """
        logger.info("Sorting by landmark similarity...")
        filename_list, _, landmarks = self._get_landmarks()
        img_list = list(zip(filename_list, landmarks))

        logger.info("Comparing landmarks and sorting...")
        img_list_len = len(img_list)
        for i in tqdm(range(0, img_list_len - 1), desc="Comparing", file=sys.stdout):
            min_score = float("inf")
            j_min_score = i + 1
            for j in range(i + 1, img_list_len):
                fl1 = img_list[i][1]
                fl2 = img_list[j][1]
                score = np.sum(np.absolute((fl2 - fl1).flatten()))
                if score < min_score:
                    min_score = score
                    j_min_score = j
            (img_list[i + 1], img_list[j_min_score]) = (img_list[j_min_score], img_list[i + 1])
        return img_list

    def sort_face_cnn_dissim(self):
        """ Sort by landmark dissimilarity """
        logger.info("Sorting by landmark dissimilarity...")
        filename_list, _, landmarks = self._get_landmarks()
        scores = np.zeros(len(filename_list), dtype='float32')
        img_list = list(list(items) for items in zip(filename_list, landmarks, scores))

        logger.info("Comparing landmarks...")
        img_list_len = len(img_list)
        for i in tqdm(range(0, img_list_len - 1), desc="Comparing", file=sys.stdout):
            score_total = 0
            for j in range(i + 1, img_list_len):
                if i == j:
                    continue
                fl1 = img_list[i][1]
                fl2 = img_list[j][1]
                score_total += np.sum(np.absolute((fl2 - fl1).flatten()))
            img_list[i][2] = score_total

        logger.info("Sorting...")
        img_list = sorted(img_list, key=operator.itemgetter(2), reverse=True)
        return img_list

    def sort_face_yaw(self):
        """ Sort by estimated face yaw angle """
        logger.info("Sorting by estimated face yaw angle..")
        filenames = []
        yaws = []
        for filename, image, metadata in tqdm(self._loader.load(),
                                              desc="Classifying Faces",
                                              total=self._loader.count,
                                              leave=False):
            if not metadata:
                msg = ("The images to be sorted do not contain alignment data. Images must have "
                       "been generated by Faceswap's Extract process.\nIf you are sorting an "
                       "older faceset, then you should re-extract the faces from your source "
                       "alignments file to generate this data.")
                raise FaceswapError(msg)
            alignments = metadata["alignments"]
            aligned_face = AlignedFace(np.array(alignments["landmarks_xy"], dtype="float32"),
                                       image=image,
                                       centering="legacy",
                                       is_aligned=True)
            filenames.append(filename)
            yaws.append(aligned_face.pose.yaw)

        logger.info("Sorting...")
        matched_list = list(zip(filenames, yaws))
        img_list = sorted(matched_list, key=operator.itemgetter(1), reverse=True)
        return img_list

    def sort_hist(self):
        """ Sort by image histogram similarity """
        logger.info("Sorting by histogram similarity...")

        # TODO We have metadata here, so we can mask the face for hist sorting
        img_list = [(filename, cv2.calcHist([image], [0], None, [256], [0, 256]))
                    for filename, image, _ in tqdm(self._loader.load(),
                                                   desc="Calculating histograms",
                                                   total=self._loader.count,
                                                   leave=False)]

        logger.info("Comparing histograms and sorting...")
        img_list_len = len(img_list)
        for i in tqdm(range(0, img_list_len - 1), desc="Comparing histograms", file=sys.stdout):
            min_score = float("inf")
            j_min_score = i + 1
            for j in range(i + 1, img_list_len):
                score = cv2.compareHist(img_list[i][1], img_list[j][1], cv2.HISTCMP_BHATTACHARYYA)
                if score < min_score:
                    min_score = score
                    j_min_score = j
            (img_list[i + 1], img_list[j_min_score]) = (img_list[j_min_score], img_list[i + 1])
        return img_list

    def sort_hist_dissim(self):
        """ Sort by image histogram dissimilarity """
        logger.info("Sorting by histogram dissimilarity...")

        # TODO We have metadata here, so we can mask the face for hist sorting
        img_list = [[filename, cv2.calcHist([image], [0], None, [256], [0, 256]), 0.0]
                    for filename, image, _ in tqdm(self._loader.load(),
                                                   desc="Calculating histograms",
                                                   total=self._loader.count,
                                                   leave=False)]

        img_list_len = len(img_list)
        for i in tqdm(range(0, img_list_len), desc="Comparing histograms", file=sys.stdout):
            score_total = 0
            for j in range(0, img_list_len):
                if i == j:
                    continue
                score_total += cv2.compareHist(img_list[i][1],
                                               img_list[j][1],
                                               cv2.HISTCMP_BHATTACHARYYA)
            img_list[i][2] = score_total

        logger.info("Sorting...")
        return sorted(img_list, key=lambda x: x[2], reverse=True)

    def sort_size(self):
        """ Sort the faces by largest face (in original frame) to smallest """
        logger.info("Sorting by original face size...")
        img_list = []
        for filename, image, metadata in tqdm(self._loader.load(),
                                              desc="Calculating face sizes",
                                              total=self._loader.count,
                                              leave=False):
            if not metadata:
                msg = ("The images to be sorted do not contain alignment data. Images must have "
                       "been generated by Faceswap's Extract process.\nIf you are sorting an "
                       "older faceset, then you should re-extract the faces from your source "
                       "alignments file to generate this data.")
                raise FaceswapError(msg)
            alignments = metadata["alignments"]
            aligned_face = AlignedFace(np.array(alignments["landmarks_xy"], dtype="float32"),
                                       image=image,
                                       centering="legacy",
                                       is_aligned=True)
            roi = aligned_face.original_roi
            size = ((roi[1][0] - roi[0][0]) ** 2 + (roi[1][1] - roi[0][1]) ** 2) ** 0.5
            img_list.append((filename, size))

        logger.info("Sorting...")
        return sorted(img_list, key=lambda x: x[1], reverse=True)

    def sort_black_pixels(self):
        """ Sort by percentage of black pixels

         Calculates the sum of black pixels, get the percentage X 3 channels
        """
        logger.info("Sorting by percentage of black pixels...")
        img_list = [(filename, np.ndarray.all(image == [0, 0, 0], axis=2).sum()/image.size*100*3)
                    for filename, image, _ in tqdm(self._loader.load(),
                                                   desc="Calculating black pixels",
                                                   total=self._loader.count,
                                                   leave=False)]
        img_list_len = len(img_list)
        for i in tqdm(range(0, img_list_len - 1), desc="Comparing black pixels", file=sys.stdout):
            for j in range(0, img_list_len-i-1):
                if img_list[j][1] > img_list[j+1][1]:
                    temp = img_list[j]
                    img_list[j] = img_list[j+1]
                    img_list[j+1] = temp
        return img_list

    # Methods for grouping
    def group_blur(self, img_list):
        """ Group into bins by blur """
        # Starting the binning process
        num_bins = self._args.num_bins

        # The last bin will get all extra images if it's
        # not possible to distribute them evenly
        num_per_bin = len(img_list) // num_bins
        remainder = len(img_list) % num_bins

        logger.info("Grouping by blur...")
        bins = [[] for _ in range(num_bins)]
        idx = 0
        for i in range(num_bins):
            for _ in range(num_per_bin):
                bins[i].append(img_list[idx][0])
                idx += 1

        # If remainder is 0, nothing gets added to the last bin.
        for i in range(1, remainder + 1):
            bins[-1].append(img_list[-i][0])

        return bins

    def group_blur_fft(self, img_list):
        """ Group into bins by fft blur score"""
        # Starting the binning process
        num_bins = self._args.num_bins

        # The last bin will get all extra images if it's
        # not possible to distribute them evenly
        num_per_bin = len(img_list) // num_bins
        remainder = len(img_list) % num_bins

        logger.info("Grouping by fft blur score...")
        bins = [[] for _ in range(num_bins)]
        idx = 0
        for i in range(num_bins):
            for _ in range(num_per_bin):
                bins[i].append(img_list[idx][0])
                idx += 1

        # If remainder is 0, nothing gets added to the last bin.
        for i in range(1, remainder + 1):
            bins[-1].append(img_list[-i][0])

        return bins

    def group_face_cnn(self, img_list):
        """ Group into bins by CNN face similarity """
        logger.info("Grouping by face-cnn similarity...")

        # Groups are of the form: group_num -> reference faces
        reference_groups = {}

        # Bins array, where index is the group number and value is
        # an array containing the file paths to the images in that group.
        bins = []

        # Comparison threshold used to decide how similar
        # faces have to be to be grouped together.
        # It is multiplied by 1000 here to allow the cli option to use smaller
        # numbers.
        min_threshold = self._args.min_threshold * 1000

        img_list_len = len(img_list)

        for i in tqdm(range(0, img_list_len - 1),
                      desc="Grouping",
                      file=sys.stdout):
            fl1 = img_list[i][1]

            current_best = [-1, float("inf")]

            for key, references in reference_groups.items():
                try:
                    score = self.get_avg_score_faces_cnn(fl1, references)
                except TypeError:
                    score = float("inf")
                except ZeroDivisionError:
                    score = float("inf")
                if score < current_best[1]:
                    current_best[0], current_best[1] = key, score

            if current_best[1] < min_threshold:
                reference_groups[current_best[0]].append(fl1[0])
                bins[current_best[0]].append(img_list[i][0])
            else:
                reference_groups[len(reference_groups)] = [img_list[i][1]]
                bins.append([img_list[i][0]])

        return bins

    def group_face_yaw(self, img_list):
        """ Group into bins by yaw of face """
        # Starting the binning process
        num_bins = self._args.num_bins

        # The last bin will get all extra images if it's
        # not possible to distribute them evenly
        num_per_bin = len(img_list) // num_bins
        remainder = len(img_list) % num_bins

        logger.info("Grouping by face-yaw...")
        bins = [[] for _ in range(num_bins)]
        idx = 0
        for i in range(num_bins):
            for _ in range(num_per_bin):
                bins[i].append(img_list[idx][0])
                idx += 1

        # If remainder is 0, nothing gets added to the last bin.
        for i in range(1, remainder + 1):
            bins[-1].append(img_list[-i][0])

        return bins

    def group_black_pixels(self, img_list):
        """ Group into bins by percentage of black pixels
        :type img_list: (str, float)
        """
        logger.info("Grouping by percentage of black pixels...")

        # Starting the binning process
        bins = [[] for _ in range(self._args.num_bins)]
        # Get edges of bins from 0 to 100
        bins_edges = self._near_split(100, self._args.num_bins)
        # Get the proper bin number for each img order
        img_bins = np.digitize([x[1] for x in img_list], bins_edges, right=True)

        # Place imgs in bins
        for idx, _bin in enumerate(img_bins):
            bins[_bin].append(img_list[idx][0])

        return bins

    def group_hist(self, img_list):
        """ Group into bins by histogram """
        logger.info("Grouping by histogram...")

        # Groups are of the form: group_num -> reference histogram
        reference_groups = {}

        # Bins array, where index is the group number and value is
        # an array containing the file paths to the images in that group
        bins = []

        min_threshold = self._args.min_threshold

        img_list_len = len(img_list)
        reference_groups[0] = [img_list[0][1]]
        bins.append([img_list[0][0]])

        for i in tqdm(range(1, img_list_len),
                      desc="Grouping",
                      file=sys.stdout):
            current_best = [-1, float("inf")]
            for key, value in reference_groups.items():
                score = self.get_avg_score_hist(img_list[i][1], value)
                if score < current_best[1]:
                    current_best[0], current_best[1] = key, score

            if current_best[1] < min_threshold:
                reference_groups[current_best[0]].append(img_list[i][1])
                bins[current_best[0]].append(img_list[i][0])
            else:
                reference_groups[len(reference_groups)] = [img_list[i][1]]
                bins.append([img_list[i][0]])

        return bins

    # Final process methods
    def final_process_rename(self, img_list):
        """ Rename the files """
        output_dir = self._args.output_dir

        process_file = self.set_process_file_method(self._args.log_changes,
                                                    self._args.keep_original)

        # Make sure output directory exists
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        description = (
            "Copying and Renaming" if self._args.keep_original
            else "Moving and Renaming"
        )

        for i in tqdm(range(0, len(img_list)),
                      desc=description,
                      leave=False,
                      file=sys.stdout):
            src = img_list[i] if isinstance(img_list[i], str) else img_list[i][0]
            src_basename = os.path.basename(src)

            dst = os.path.join(output_dir, f"{i:05d}_{src_basename}")
            try:
                process_file(src, dst, self.changes)
            except FileNotFoundError as err:
                logger.error(err)
                logger.error('fail to rename %s', src)

        for i in tqdm(range(0, len(img_list)),
                      desc=description,
                      file=sys.stdout):
            renaming = self.set_renaming_method(self._args.log_changes)
            fname = img_list[i] if isinstance(img_list[i], str) else img_list[i][0]
            src, dst = renaming(fname, output_dir, i, self.changes)

            try:
                os.rename(src, dst)
            except FileNotFoundError as err:
                logger.error(err)
                logger.error('fail to rename %s', format(src))

        if self._args.log_changes:
            self.write_to_log(self.changes)

    def final_process_folders(self, bins):
        """ Move the files to folders """
        output_dir = self._args.output_dir

        process_file = self.set_process_file_method(self._args.log_changes,
                                                    self._args.keep_original)

        # First create new directories to avoid checking
        # for directory existence in the moving loop
        logger.info("Creating group directories.")
        for i in range(len(bins)):
            directory = os.path.join(output_dir, str(i))
            if not os.path.exists(directory):
                os.makedirs(directory)

        description = (
            "Copying into Groups" if self._args.keep_original
            else "Moving into Groups"
        )

        logger.info("Total groups found: %s", len(bins))
        for i in tqdm(range(len(bins)), desc=description, file=sys.stdout):
            for j in range(len(bins[i])):
                src = bins[i][j]
                src_basename = os.path.basename(src)

                dst = os.path.join(output_dir, str(i), src_basename)
                try:
                    process_file(src, dst, self.changes)
                except FileNotFoundError as err:
                    logger.error(err)
                    logger.error("Failed to move '%s' to '%s'", src, dst)

        if self._args.log_changes:
            self.write_to_log(self.changes)

    # Various helper methods
    def write_to_log(self, changes):
        """ Write the changes to log file """
        logger.info("Writing sort log to: '%s'", self._args.log_file_path)
        self.serializer.save(self._args.log_file_path, changes)

    def reload_images(self, group_method, img_list):
        """
        Reloads the image list by replacing the comparative values with those
        that the chosen grouping method expects.
        :param group_method: str name of the grouping method that will be used.
        :param img_list: image list that has been sorted by one of the sort
        methods.
        :return: img_list but with the comparative values that the chosen
        grouping method expects.
        """
        logger.info("Preparing to group...")
        if group_method == 'group_blur':
            filename_list, image_list = self._get_images()
            blurs = [self.estimate_blur(img) for img in image_list]
            temp_list = list(zip(filename_list, blurs))
        elif group_method == 'group_blur_fft':
            filename_list, image_list = self._get_images()
            fft_blurs = [self.estimate_blur_fft(img) for img in image_list]
            temp_list = list(zip(filename_list, fft_blurs))
        elif group_method == 'group_face_cnn':
            filename_list, image_list, landmarks = self._get_landmarks()
            temp_list = list(zip(filename_list, landmarks))
        elif group_method == 'group_face_yaw':
            filename_list, image_list, landmarks = self._get_landmarks()
            yaws = [self.calc_landmarks_face_yaw(mark) for mark in landmarks]
            temp_list = list(zip(filename_list, yaws))
        elif group_method == 'group_hist':
            filename_list, image_list = self._get_images()
            histograms = [cv2.calcHist([img], [0], None, [256], [0, 256]) for img in image_list]
            temp_list = list(zip(filename_list, histograms))
        elif group_method == 'group_black_pixels':
            filename_list, image_list = self._get_images()
            black_pixels = [np.ndarray.all(img == [0, 0, 0], axis=2).sum()/img.size*100*3
                            for img in image_list]
            temp_list = list(zip(filename_list, black_pixels))
        else:
            raise ValueError(f"{group_method} group_method not found.")

        return self.splice_lists(img_list, temp_list)

    @staticmethod
    def _near_split(bin_range, num_bins):
        """ Obtain the split for the given number of bins for the given range

        Parameters
        ----------
        bin_range: int
            The range of data to separate into bins
        num_bins: int
            The number of bins to create

        Returns
        -------
        list
            The split dividers for the given number of bins for the given range
        """
        quotient, remainder = divmod(bin_range, num_bins)
        seps = [quotient + 1] * remainder + [quotient] * (num_bins - remainder)
        uplimit = 0
        bins = [0]
        for sep in seps:
            bins.append(uplimit + sep)
            uplimit += sep
        return bins

    @staticmethod
    def _convert_color(imgs, same_size, method):
        """ Helper function to convert color spaces """

        if method.endswith('gray'):
            conversion = np.array([[0.0722], [0.7152], [0.2126]])
        else:
            conversion = np.array([[0.25, 0.5, 0.25], [-0.5, 0.0, 0.5], [-0.25, 0.5, -0.25]])

        if same_size:
            path = 'greedy'
            operation = 'bijk, kl -> bijl' if method.endswith('gray') else 'bijl, kl -> bijk'
        else:
            operation = 'ijk, kl -> ijl' if method.endswith('gray') else 'ijl, kl -> ijk'
            path = np.einsum_path(operation, imgs[0][..., :3], conversion, optimize='optimal')[0]

        progress_bar = tqdm(imgs, desc="Converting", file=sys.stdout)
        images = [np.einsum(operation, img[..., :3], conversion, optimize=path).astype('float32')
                  for img in progress_bar]
        return images

    @staticmethod
    def splice_lists(sorted_list, new_vals_list):
        """
        This method replaces the value at index 1 in each sub-list in the
        sorted_list with the value that is calculated for the same img_path,
        but found in new_vals_list.

        Format of lists: [[img_path, value], [img_path2, value2], ...]

        :param sorted_list: list that has been sorted by one of the sort
        methods.
        :param new_vals_list: list that has been loaded by a different method
        than the sorted_list.
        :return: list that is sorted in the same way as the input sorted list
        but the values corresponding to each image are from new_vals_list.
        """
        new_list = []
        # Make new list of just image paths to serve as an index
        val_index_list = [i[0] for i in new_vals_list]
        for i in tqdm(range(len(sorted_list)), desc="Splicing", file=sys.stdout):
            current_img = sorted_list[i] if isinstance(sorted_list[i], str) else sorted_list[i][0]
            new_val_index = val_index_list.index(current_img)
            new_list.append([current_img, new_vals_list[new_val_index][1]])

        return new_list

    @staticmethod
    def find_images(input_dir):
        """ Return list of images at specified location """
        result = []
        extensions = [".jpg", ".png", ".jpeg"]
        for root, _, files in os.walk(input_dir):
            for file in files:
                if os.path.splitext(file)[1].lower() in extensions:
                    result.append(os.path.join(root, file))
            break
        return result

    @classmethod
    def estimate_blur(cls, image, metadata=None):
        """ Estimate the amount of blur an image has with the variance of the Laplacian.
        Normalize by pixel number to offset the effect of image size on pixel gradients & variance.

        Parameters
        ----------
        image: :class:`numpy.ndarray`
            The face image to calculate blur for
        metadata: dict, optional
            The metadata for the face image or ``None`` if no metadata is available. If metadata is
            provided the face will be masked by the "components" mask prior to calculating blur.
            Default:``None``

        Returns
        -------
        float
            The estimated blur score for the face
        """
        if metadata is not None:
            alignments = metadata["alignments"]
            det_face = DetectedFace()
            det_face.from_png_meta(alignments)
            aln_face = AlignedFace(np.array(alignments["landmarks_xy"], dtype="float32"),
                                   image=image,
                                   centering="legacy",
                                   size=256,
                                   is_aligned=True)
            mask = det_face.mask["components"]
            mask.set_sub_crop(aln_face.pose.offset[mask.stored_centering],
                              aln_face.pose.offset["legacy"],
                              centering="legacy")
            mask = cv2.resize(mask.mask, (256, 256), interpolation=cv2.INTER_CUBIC)[..., None]
            image = np.minimum(aln_face.face, mask)
        if image.ndim == 3:
            image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        blur_map = cv2.Laplacian(image, cv2.CV_32F)
        score = np.var(blur_map) / np.sqrt(image.shape[0] * image.shape[1])
        return score

    @classmethod
    def estimate_blur_fft(cls, image, metadata=None):
        """ Estimate the amount of blur a fft filtered image has.

        Parameters
        ----------
        image: :class:`numpy.ndarray`
            Use Fourier Transform to analyze the frequency characteristics of the masked
            face using 2D Discrete Fourier Transform (DFT) filter to find the frequency domain.
            A mean value is assigned to the magnitude spectrum and returns a blur score.
            Adapted from https://www.pyimagesearch.com/2020/06/15/
            opencv-fast-fourier-transform-fft-for-blur-detection-in-images-and-video-streams/
        metadata: dict, optional
            The metadata for the face image or ``None`` if no metadata is available. If metadata is
            provided the face will be masked by the "components" mask prior to calculating blur.
            Default:``None``

        Returns
        -------
        float
            The estimated fft blur score for the face
        """
        if metadata is not None:
            alignments = metadata["alignments"]
            det_face = DetectedFace()
            det_face.from_png_meta(alignments)
            aln_face = AlignedFace(np.array(alignments["landmarks_xy"], dtype="float32"),
                                   image=image,
                                   centering="legacy",
                                   size=256,
                                   is_aligned=True)
            mask = det_face.mask["components"]
            mask.set_sub_crop(aln_face.pose.offset[mask.stored_centering],
                              aln_face.pose.offset["legacy"],
                              centering="legacy")
            mask = cv2.resize(mask.mask, (256, 256), interpolation=cv2.INTER_CUBIC)[..., None]
            image = np.minimum(aln_face.face, mask)
        if image.ndim == 3:
            image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        height, width = image.shape
        c_height, c_width = (int(height / 2.0), int(width / 2.0))
        fft = np.fft.fft2(image)
        fft_shift = np.fft.fftshift(fft)
        fft_shift[c_height - 75:c_height + 75, c_width - 75:c_width + 75] = 0
        ifft_shift = np.fft.ifftshift(fft_shift)
        shift_back = np.fft.ifft2(ifft_shift)
        magnitude = np.log(np.abs(shift_back))
        score = np.mean(magnitude)
        return score

    @staticmethod
    def calc_landmarks_face_pitch(flm):
        """ UNUSED - Calculate the amount of pitch in a face """
        var_t = ((flm[6][1] - flm[8][1]) + (flm[10][1] - flm[8][1])) / 2.0
        var_b = flm[8][1]
        return var_b - var_t

    @staticmethod
    def calc_landmarks_face_yaw(flm):
        """ Calculate the amount of yaw in a face """
        var_l = ((flm[27][0] - flm[0][0])
                 + (flm[28][0] - flm[1][0])
                 + (flm[29][0] - flm[2][0])) / 3.0
        var_r = ((flm[16][0] - flm[27][0])
                 + (flm[15][0] - flm[28][0])
                 + (flm[14][0] - flm[29][0])) / 3.0
        return var_r - var_l

    @staticmethod
    def set_process_file_method(log_changes, keep_original):
        """
        Assigns the final file processing method based on whether changes are
        being logged and whether the original files are being kept in the
        input directory.
        Relevant cli arguments: -k, -l
        :return: function reference
        """
        if log_changes:
            if keep_original:
                def process_file(src, dst, changes):
                    """ Process file method if logging changes
                        and keeping original """
                    copyfile(src, dst)
                    changes[src] = dst

            else:
                def process_file(src, dst, changes):
                    """ Process file method if logging changes
                        and not keeping original """
                    os.rename(src, dst)
                    changes[src] = dst

        else:
            if keep_original:
                def process_file(src, dst, changes):  # pylint: disable=unused-argument
                    """ Process file method if not logging changes
                        and keeping original """
                    copyfile(src, dst)

            else:
                def process_file(src, dst, changes):  # pylint: disable=unused-argument
                    """ Process file method if not logging changes
                        and not keeping original """
                    os.rename(src, dst)
        return process_file

    @staticmethod
    def set_renaming_method(log_changes):
        """ Set the method for renaming files """
        if log_changes:
            def renaming(src, output_dir, i, changes):
                """ Rename files  method if logging changes """
                src_basename = os.path.basename(src)

                __src = os.path.join(output_dir,
                                     f"{i:05d}_{src_basename}")
                dst = os.path.join(
                    output_dir,
                    f"{i:05d}{os.path.splitext(src_basename)[1]}")
                changes[src] = dst
                return __src, dst
        else:
            def renaming(src, output_dir, i, changes):  # pylint: disable=unused-argument
                """ Rename files method if not logging changes """
                src_basename = os.path.basename(src)

                src = os.path.join(output_dir,
                                   f"{i:05d}_{src_basename}")
                dst = os.path.join(
                    output_dir,
                    f"{i:05d}{os.path.splitext(src_basename)[1]}")
                return src, dst
        return renaming

    @staticmethod
    def get_avg_score_hist(img1, references):
        """ Return the average histogram score between a face and
            reference image """
        scores = []
        for img2 in references:
            score = cv2.compareHist(img1, img2, cv2.HISTCMP_BHATTACHARYYA)
            scores.append(score)
        return sum(scores) / len(scores)

    @staticmethod
    def get_avg_score_faces_cnn(fl1, references):
        """ Return the average CNN similarity score
            between a face and reference image """
        scores = []
        for fl2 in references:
            score = np.sum(np.absolute((fl2 - fl1).flatten()))
            scores.append(score)
        return sum(scores) / len(scores)
