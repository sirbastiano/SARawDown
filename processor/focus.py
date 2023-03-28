import matplotlib.pyplot as plt
import cmath
import numpy as np
import math as math
import os, sys
import sentinel1decoder
import sentinel1decoder.constants
import sentinel1decoder.utilities
import logging
from scipy.interpolate import interp1d
import argparse

class Focus:
    def __init__(self, decoder, raw, ephemeris):
        self.decoder = decoder
        self.selection = raw
        self.ephemeris = ephemeris
        self.initialize_parameters()

    def initialize_parameters(self):
        """Initialize necessary parameters as None."""
        self.iq_array = None
        self.len_range_line = None
        self.len_az_line = None
        self.range_sample_freq = None
        self.range_sample_period = None
        self.az_sample_freq = None
        self.az_sample_period = None
        self.fast_time = None
        self.slant_range = None
        self.az_freq_vals = None
        self.range_freq_vals = None

    def decode_file(self):
        """Decode the SAR data file and store the IQ data in iq_array."""
        self.iq_array = self.decoder.decode_file(self.selection)
        print("Raw data shape: ", self.iq_array.shape)
        self.len_range_line = self.iq_array.shape[1]
        self.len_az_line = self.iq_array.shape[0]

    def extract_parameters(self):
        """Extract necessary parameters from the selection dataframe."""
        self.c = sentinel1decoder.constants.speed_of_light
        self.TXPL = self.selection["Tx Pulse Length"].unique()[0]
        self.TXPSF = self.selection["Tx Pulse Start Frequency"].unique()[0]
        self.TXPRR = self.selection["Tx Ramp Rate"].unique()[0]
        self.RGDEC = self.selection["Range Decimation"].unique()[0]
        self.PRI = self.selection["PRI"].unique()[0]
        self.rank = self.selection["Rank"].unique()[0]
        self.suppressed_data_time = 320 / (8 * sentinel1decoder.constants.f_ref)
        self.range_start_time = self.selection["SWST"].unique()[0] + self.suppressed_data_time

    def calculate_wavelength(self):
        """Calculate the SAR radar wavelength."""
        self.wavelength = self.c / 5.405e9

    def calculate_sample_rates(self):
        """Calculate sample rates and periods for range and azimuth."""
        self.range_sample_freq = sentinel1decoder.utilities.range_dec_to_sample_rate(self.RGDEC)
        self.range_sample_period = 1 / self.range_sample_freq
        self.az_sample_freq = 1 / self.PRI
        self.az_sample_period = self.PRI

    def create_fast_time_vector(self):
        """Create the fast time vector."""
        range_line_num = np.arange(self.len_range_line)
        self.fast_time = self.range_start_time + range_line_num * self.range_sample_period

    def calculate_slant_range(self):
        """Calculate the slant range vector."""
        self.slant_range = (self.rank * self.PRI + self.fast_time) * self.c / 2

    def calculate_axes(self):
        """Calculate frequency axes for range and azimuth after FFT."""
        SWL = self.len_range_line / self.range_sample_freq
        self.az_freq_vals = np.arange(-self.az_sample_freq / 2, self.az_sample_freq / 2, 1 / (self.PRI * self.len_az_line))
        self.range_freq_vals = np.arange(-self.range_sample_freq / 2, self.range_sample_freq / 2, 1)
                                         
    @staticmethod
    def d(range_freq, velocity, wavelength):
        """Calculate the D factor."""
        return math.sqrt(1 - ((wavelength ** 2 * range_freq ** 2) / (4 * velocity ** 2)))

    def calculate_spacecraft_velocity(self):
        """Calculate the spacecraft velocity."""
        self.D = np.zeros((self.len_az_line, self.len_range_line))

        ecef_vels = self.ephemeris.apply(lambda x: math.sqrt(
            x["X-axis velocity ECEF"] ** 2 + x["Y-axis velocity ECEF"] ** 2 + x["Z-axis velocity ECEF"] ** 2), axis=1)
        velocity_interp = interp1d(self.ephemeris["POD Solution Data Timestamp"].unique(), ecef_vels.unique(),
                                   fill_value="extrapolate")
        self.x_interp = interp1d(self.ephemeris["POD Solution Data Timestamp"].unique(), self.ephemeris["X-axis position ECEF"].unique(),
                            fill_value="extrapolate")
        self.y_interp = interp1d(self.ephemeris["POD Solution Data Timestamp"].unique(), self.ephemeris["Y-axis position ECEF"].unique(),
                            fill_value="extrapolate")
        self.z_interp = interp1d(self.ephemeris["POD Solution Data Timestamp"].unique(), self.ephemeris["Z-axis position ECEF"].unique(),
                            fill_value="extrapolate")
        self.space_velocities = self.selection.apply(lambda x: velocity_interp(x["Coarse Time"] + x["Fine Time"]), axis=1)


    def calculate_positions(self):
        """Calculate x, y, and z positions for each azimuth line."""
        self.x_positions = self.selection.apply(lambda x: self.x_interp(x["Coarse Time"] + x["Fine Time"]), axis=1).to_list()
        self.y_positions = self.selection.apply(lambda x: self.y_interp(x["Coarse Time"] + x["Fine Time"]), axis=1).to_list()
        self.z_positions = self.selection.apply(lambda x: self.z_interp(x["Coarse Time"] + x["Fine Time"]), axis=1).to_list()

    def calculate_velocity_and_d(self):
        """Calculate spacecraft velocities and D factors for each azimuth and range line."""
        a = 6378137  # WGS84 semi-major axis
        b = 6356752.3142  # WGS84 semi-minor axis
        self.velocities = np.zeros((self.len_az_line, self.len_range_line))
        self.D = np.zeros((self.len_az_line, self.len_range_line))

        for i in range(self.len_az_line):
            H = math.sqrt(self.x_positions[i] ** 2 + self.y_positions[i] ** 2 + self.z_positions[i] ** 2)
            W = float(self.space_velocities.iloc[i]) / H
            lat = math.atan(self.z_positions[i] / self.x_positions[i])
            local_earth_rad = math.sqrt(((a ** 2 * math.cos(lat)) ** 2 + (b ** 2 * math.sin(lat)) ** 2) /
                                        ((a * math.cos(lat)) ** 2 + (b * math.sin(lat)) ** 2))
            for j in range(self.len_range_line):
                cos_beta = (local_earth_rad ** 2 + H ** 2 - self.slant_range[j] ** 2) / (2 * local_earth_rad * H)
                this_ground_velocity = local_earth_rad * W * cos_beta
                self.velocities[i, j] = math.sqrt(float(self.space_velocities.iloc[i]) * this_ground_velocity)
                self.D[i, j] = self.d(self.az_freq_vals[i], self.velocities[i, j], self.wavelength)

    def process_freq_domain_data(self):
        """Process frequency domain data."""
        self.freq_domain_data = np.zeros((self.len_az_line, self.len_range_line), dtype=complex)

        for az_index in range(self.len_az_line):
            range_line = self.iq_array[az_index, :]
            range_fft = np.fft.fft(range_line)
            self.freq_domain_data[az_index, :] = range_fft

        for range_index in range(self.len_range_line):
            az_line = self.freq_domain_data[:, range_index]
            az_fft = np.fft.fft(az_line)
            az_fft = np.fft.fftshift(az_fft)
            self.freq_domain_data[:, range_index] = az_fft


    def apply_range_filter(self):
        """Apply the range filter to the frequency domain data."""
        num_tx_vals = int(self.TXPL * self.range_sample_freq)
        tx_replica_time_vals = np.linspace(-self.TXPL / 2, self.TXPL / 2, num=num_tx_vals)
        phi1 = self.TXPSF + self.TXPRR * self.TXPL / 2
        phi2 = self.TXPRR / 2
        tx_replica = np.zeros(num_tx_vals, dtype=complex)
        for i in range(num_tx_vals):
            tx_replica[i] = cmath.exp(2j * cmath.pi * (phi1 * tx_replica_time_vals[i] + phi2 * tx_replica_time_vals[i] ** 2))

        range_filter = np.zeros(self.len_range_line, dtype=complex)
        index_start = np.ceil((self.len_range_line - num_tx_vals) / 2) - 1
        index_end = num_tx_vals + np.ceil((self.len_range_line - num_tx_vals) / 2) - 2
        range_filter[int(index_start):int(index_end + 1)] = tx_replica

        range_filter = np.fft.fft(range_filter)
        range_filter = np.conjugate(range_filter)

        for az_index in range(self.len_az_line):
            self.freq_domain_data[az_index, :] = self.freq_domain_data[az_index, :] * range_filter
    
    
    def apply_rcmc_filter(self):
        """Apply the RCMC filter to the frequency domain data."""
        rcmc_filt = np.zeros(self.len_range_line, dtype=complex)
        range_freq_vals = np.linspace(-self.range_sample_freq / 2, self.range_sample_freq / 2, num=self.len_range_line)
        for az_index in range(self.len_az_line):
            rcmc_filt = np.zeros(self.len_range_line, dtype=complex)
            for range_index in range(self.len_range_line):
                rcmc_shift = self.slant_range[0] * ((1 / self.D[az_index, range_index]) - 1)
                rcmc_filt[range_index] = cmath.exp(4j * cmath.pi * range_freq_vals[range_index] * rcmc_shift / self.c)
            self.freq_domain_data[az_index, :] = self.freq_domain_data[az_index, :] * rcmc_filt

        self.range_doppler_data = np.zeros((self.len_az_line, self.len_range_line), dtype=complex)
        for range_line_index in range(self.len_az_line):
            ifft = np.fft.ifft(self.freq_domain_data[range_line_index, :])
            ifft_sorted = np.fft.ifftshift(ifft)
            self.range_doppler_data[range_line_index, :] = ifft_sorted

    def apply_azimuth_filter(self):
        """Apply the azimuth filter and create the compressed data."""
        self.az_compressed_data = np.zeros((self.len_az_line, self.len_range_line), 'complex')

        for az_line_index in range(self.len_range_line):
            d_vector = np.zeros(self.len_az_line)
            
            this_az_filter = np.zeros(self.len_az_line, 'complex')
            for i in range(len(self.az_freq_vals) - 2):  # -1
                this_az_filter[i] = cmath.exp(
                    (4j * cmath.pi * self.slant_range[i] * self.D[i, az_line_index]) / self.wavelength)
            result = self.range_doppler_data[:, az_line_index] * this_az_filter[:]
            result = np.fft.ifft(result)
            self.az_compressed_data[:, az_line_index] = result

    def plot_img(self):
        # Plot final image
        plt.figure(figsize=(16,100))
        plt.title("Sentinel-1 Processed SAR Image")
        plt.imshow(abs(self.az_compressed_data[:,:]), vmin=0, vmax=2000, origin='lower')
        plt.xlabel("Down Range (samples)")
        plt.ylabel("Cross Range (samples)")
        plt.show()

    def process(self):
        """Main processing method."""
        self.decode_file()
        self.extract_parameters()
        self.calculate_wavelength()
        self.calculate_sample_rates()
        self.create_fast_time_vector()
        self.calculate_slant_range()
        self.calculate_axes()
        self.calculate_spacecraft_velocity()
        self.calculate_positions()
        self.calculate_velocity_and_d()
        self.process_freq_domain_data()
        self.apply_range_filter()
        self.apply_rcmc_filter()
        self.apply_azimuth_filter()
        self.plot_img()


if __name__ == "__main__":
       # Create argument parser
       parser = argparse.ArgumentParser(description="Sentinel-1 Level 0 Decoder")
       parser.add_argument("--input_file", help="Input file")
       parser.add_argument("--swath_number", help="Swath number")
       
       try:
           args = parser.parse_args()
       except:
           logging.error("Wrong arguments")
           sys.exit(1)
       logging.info("Arguments have been parsed")
       
       input_file = args.input_file
       swath_number = int(args.swath_number)
       
       
       # Create object
       decoder = sentinel1decoder.Level0Decoder(input_file, log_level=logging.WARNING)
       df = decoder.decode_metadata()
       df[df["Swath Number"] == swath_number]
       ephemeris = sentinel1decoder.utilities.read_subcommed_data(df)
       # TODO: script to check the shapes of the dataframes
       selection = df.iloc[50:1000]  # from 61 it works
       F = Focus(decoder, selection, ephemeris)
       logging.info(f"selected {F.selection.shape[0]} lines")
       F.process()
       print("Done")