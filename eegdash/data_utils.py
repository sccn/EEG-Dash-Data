import os
import sys 
from joblib import Parallel, delayed
import mne
import numpy as np
from pathlib import Path
import re
import json
from mne.io import BaseRaw
from mne._fiff.utils import _find_channels, _read_segments_file
import s3fs
import tempfile
from mne._fiff.utils import _read_segments_file

class RawEEGDash(BaseRaw):
    r"""Raw object from EEG-Dash connection with Openneuro S3 file.

    Parameters
    ----------
    input_fname : path-like
        Path to the S3 file
    eog : list | tuple | 'auto'
        Names or indices of channels that should be designated EOG channels.
        If 'auto', the channel names containing ``EOG`` or ``EYE`` are used.
        Defaults to empty tuple.
    %(preload)s
        Note that preload=False will be effective only if the data is stored
        in a separate binary file.
    %(uint16_codec)s
    %(montage_units)s
    %(verbose)s

    See Also
    --------
    mne.io.Raw : Documentation of attributes and methods.

    Notes
    -----
    .. versionadded:: 0.11.0
    """

    def __init__(
        self,
        input_fname,
        metadata,
        eog=(),
        preload=False,
        *,
        cache_dir='.',
        uint16_codec=None,
        montage_units="auto",
        verbose=None,
    ):
        '''
        Get to work with S3 endpoint first, no caching
        '''
        # Create a simple RawArray
        sfreq = metadata['sfreq']  # Sampling frequency
        n_chans = metadata['nchans']
        n_times = metadata['n_times']
        print('n_times', n_times)
        ch_names = [f'EEG{d}' for d in range(1,n_chans+1)]
        ch_types = ["eeg"] * n_chans
        info = mne.create_info(ch_names=ch_names, sfreq=sfreq, ch_types=ch_types)
        self.s3file = input_fname
        self.filecache = os.path.join(cache_dir, os.path.basename(self.s3file))

        if preload and not os.path.exists(self.filecache):
            self._download_s3()
            preload = self.filecache

        super().__init__(
            info,
            preload,
            last_samps=[n_times-1],
            orig_format="double",
            verbose=verbose,
        )

    def _download_s3(self):
        filesystem = s3fs.S3FileSystem(anon=True, client_kwargs={'region_name': 'us-east-2'})
        filesystem.download(self.s3file, self.filecache)
        self.filenames = [self.filecache]

    def _read_segment(
        self, start=0, stop=None, sel=None, data_buffer=None, *, verbose=None
    ):
        if not os.path.exists(self.filecache): # not preload
            self._download_s3()
        else: # not preload and file is not cached
            self.filenames = [self.filecache]
        return super()._read_segment(start, stop, sel, data_buffer, verbose=verbose)
    
    def _read_segment_file(self, data, idx, fi, start, stop, cals, mult):
        """Read a chunk of data from the file."""
        _read_segments_file(self, data, idx, fi, start, stop, cals, mult, dtype="<f4")


class BIDSDataset():
    ALLOWED_FILE_FORMAT = ['eeglab', 'brainvision', 'biosemi', 'european']
    RAW_EXTENSION = {
        'eeglab': '.set',
        'brainvision': '.vhdr',
        'biosemi': '.bdf',
        'european': '.edf'
    }
    METADATA_FILE_EXTENSIONS = ['eeg.json', 'channels.tsv', 'electrodes.tsv', 'events.tsv', 'events.json']
    def __init__(self,
            data_dir=None,                            # location of asr cleaned data 
            dataset='',                               # dataset name
            raw_format='eeglab',                      # format of raw data
        ):                            
        if data_dir is None or not os.path.exists(data_dir):
            raise ValueError('data_dir must be specified and must exist')
        self.bidsdir = Path(data_dir)
        self.dataset = dataset

        if raw_format.lower() not in self.ALLOWED_FILE_FORMAT:
            raise ValueError('raw_format must be one of {}'.format(self.ALLOWED_FILE_FORMAT))
        self.raw_format = raw_format.lower()

        # get all .set files in the bids directory
        temp_dir = (Path().resolve() / 'data')
        if not os.path.exists(temp_dir):
            os.mkdir(temp_dir)
        if not os.path.exists(temp_dir / f'{dataset}_files.npy'):
            self.files = self.get_files_with_extension_parallel(self.bidsdir, extension=self.RAW_EXTENSION[self.raw_format])
            np.save(temp_dir / f'{dataset}_files.npy', self.files)
        else:
            self.files = np.load(temp_dir / f'{dataset}_files.npy', allow_pickle=True)

    def get_property_from_filename(self, property, filename):
        import platform
        if platform.system() == "Windows":
            lookup = re.search(rf'{property}-(.*?)[_\\]', filename)
        else:
            lookup = re.search(rf'{property}-(.*?)[_\/]', filename)
        return lookup.group(1) if lookup else ''

    def get_bids_file_inheritance(self, path, basename, extension):
        '''
        Get all files with given extension that applies to the basename file 
        following the BIDS inheritance principle in the order of lowest level first
        @param
            basename: bids file basename without _eeg.set extension for example
            extension: e.g. channels.tsv
        '''
        top_level_files = ['README', 'dataset_description.json', 'participants.tsv']
        bids_files = []

        # check if path is str object
        if isinstance(path, str):
            path = Path(path)
        if not path.exists:
            raise ValueError('path {path} does not exist')

        # check if file is in current path
        for file in os.listdir(path):
            # target_file = path / f"{cur_file_basename}_{extension}"
            if os.path.isfile(path/file):
                cur_file_basename = file[:file.rfind('_')]
                if file.endswith(extension) and cur_file_basename in basename:
                    filepath = path / file
                    bids_files.append(filepath)

        # check if file is in top level directory
        if any(file in os.listdir(path) for file in top_level_files):
            return bids_files
        else:
            # call get_bids_file_inheritance recursively with parent directory
            bids_files.extend(self.get_bids_file_inheritance(path.parent, basename, extension))
            return bids_files

    def get_bids_metadata_files(self, filepath, metadata_file_extension):
        """
        (Wrapper for self.get_bids_file_inheritance)
        Get all BIDS metadata files that are associated with the given filepath, following the BIDS inheritance principle.
        
        Args:
            filepath (str or Path): The filepath to get the associated metadata files for.
            metadata_files_extensions (list): A list of file extensions to search for metadata files.
        
        Returns:
            list: A list of filepaths for all the associated metadata files
        """
        if isinstance(filepath, str):
            filepath = Path(filepath)
        if not filepath.exists:
            raise ValueError('filepath {filepath} does not exist')
        path, filename = os.path.split(filepath)
        basename = filename[:filename.rfind('_')]
        # metadata files
        meta_files = self.get_bids_file_inheritance(path, basename, metadata_file_extension)
        if not meta_files:
            raise ValueError('No metadata files found for filepath {filepath} and extension {metadata_file_extension}')
        else:
            return meta_files
        
    def scan_directory(self, directory, extension):
        result_files = []
        directory_to_ignore = ['.git']
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_file() and entry.name.endswith(extension):
                    print('Adding ', entry.path)
                    result_files.append(entry.path)
                elif entry.is_dir():
                    # check that entry path doesn't contain any name in ignore list
                    if not any(name in entry.name for name in directory_to_ignore):
                        result_files.append(entry.path)  # Add directory to scan later
        return result_files

    def get_files_with_extension_parallel(self, directory, extension='.set', max_workers=-1):
        result_files = []
        dirs_to_scan = [directory]

        # Use joblib.Parallel and delayed to parallelize directory scanning
        while dirs_to_scan:
            print(f"Scanning {len(dirs_to_scan)} directories...", dirs_to_scan)
            # Run the scan_directory function in parallel across directories
            results = Parallel(n_jobs=max_workers, prefer="threads", verbose=1)(
                delayed(self.scan_directory)(d, extension) for d in dirs_to_scan
            )
            
            # Reset the directories to scan and process the results
            dirs_to_scan = []
            for res in results:
                for path in res:
                    if os.path.isdir(path):
                        dirs_to_scan.append(path)  # Queue up subdirectories to scan
                    else:
                        result_files.append(path)  # Add files to the final result
            print(f"Current number of files: {len(result_files)}")

        return result_files

    def load_and_preprocess_raw(self, raw_file, preprocess=False):
        print(f"Loading {raw_file}")
        EEG = mne.io.read_raw_eeglab(raw_file, preload=True, verbose='error')
        
        if preprocess:
            # highpass filter
            EEG = EEG.filter(l_freq=0.25, h_freq=25, verbose=False)
            # remove 60Hz line noise
            EEG = EEG.notch_filter(freqs=(60), verbose=False)
            # bring to common sampling rate
            sfreq = 128
            if EEG.info['sfreq'] != sfreq:
                EEG = EEG.resample(sfreq)
            # # normalize data to zero mean and unit variance
            # scalar = preprocessing.StandardScaler()
            # mat_data = scalar.fit_transform(mat_data.T).T # scalar normalize for each feature and expects shape data x features

        mat_data = EEG.get_data()

        if len(mat_data.shape) > 2:
            raise ValueError('Expect raw data to be CxT dimension')
        return mat_data
    
    def get_files(self):
        return self.files
    
    def resolve_bids_json(self, json_files: list):
        """
        Resolve the BIDS JSON files and return a dictionary of the resolved values.
        Args:
            json_files (list): A list of JSON files to resolve in order of leaf level first

        Returns:
            dict: A dictionary of the resolved values.
        """
        if len(json_files) == 0:
            raise ValueError('No JSON files provided')
        json_files.reverse() # TODO undeterministic

        json_dict = {}
        for json_file in json_files:
            with open(json_file) as f:
                json_dict.update(json.load(f))
        return json_dict

    def sfreq(self, data_filepath):
        json_files = self.get_bids_metadata_files(data_filepath, 'eeg.json')
        if len(json_files) == 0:
            raise ValueError('No eeg.json found')

        metadata = self.resolve_bids_json(json_files)
        if 'SamplingFrequency' not in metadata:
            raise ValueError('SamplingFrequency not found in metadata')
        else:
            return metadata['SamplingFrequency']
    
    def task(self, data_filepath):
        return self.get_property_from_filename('task', data_filepath)
        
    def session(self, data_filepath):
        return self.get_property_from_filename('session', data_filepath)

    def run(self, data_filepath):
        return self.get_property_from_filename('run', data_filepath)

    def subject(self, data_filepath):
        return self.get_property_from_filename('sub', data_filepath)

    def num_channels(self, data_filepath):
        EEG = mne.io.read_raw_eeglab(data_filepath, preload=False, verbose='error')
        return EEG.info["nchan"]

    def channel_labels(self, data_filepath):
        EEG = mne.io.read_raw_eeglab(data_filepath, preload=False, verbose='error')
        return EEG.info['ch_names']
    
    def channel_types(self, data_filepath):
        EEG = mne.io.read_raw_eeglab(data_filepath, preload=False, verbose='error')
        return [mne.channel_type(EEG.info, idx) for idx in mne.pick_channels(EEG.info["ch_names"], [])]
            
    def num_times(self, data_filepath):
        EEG = mne.io.read_raw_eeglab(data_filepath, preload=False, verbose='error')
        return int(EEG.n_times)