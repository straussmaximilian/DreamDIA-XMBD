import re
import os
import os.path
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('pdf')
import matplotlib.pyplot as plt
from sklearn.preprocessing import MinMaxScaler
from sklearn.linear_model import LinearRegression
from sklearn.linear_model import RANSACRegressor
from keras.models import load_model
from scipy.stats import pearsonr
import tools_cython as tools
import statsmodels.api as sm
from mz_calculator import calc_fragment_mz, calc_all_fragment_mzs
from utils import calc_win_id, calc_XIC, filter_matrix, adjust_size, calc_pearson, calc_pearson_sums, adjust_cycle
class IRT_Precursor:
    def __init__(self, precursor_id, full_sequence, charge, precursor_mz, iRT, protein_name):
        self.precursor_id = precursor_id
        self.full_sequence = full_sequence
        self.sequence = re.sub(r"\(UniMod:\d+\)", "", full_sequence)
        self.charge = charge
        self.precursor_mz = precursor_mz
        self.iRT = iRT
        self.protein_name = protein_name 
    def __eq__(self, obj):
        return (self.full_sequence == obj.full_sequence) and (self.charge == obj.charge)
    def filter_frags(self, frag_list, mz_min, mz_max, padding = False, padding_value = -1):
        if padding:
            return list(map(lambda x : x if (mz_min <= x < mz_max) else padding_value, frag_list))
        return [i for i in frag_list if mz_min <= i < mz_max]
    def __calc_self_frags(self, mz_min, mz_max):
        self.self_frags, self.self_frag_charges = np.array(calc_all_fragment_mzs(self.full_sequence, 
                                                                                 self.charge, 
                                                                                 (mz_min, mz_max),  
                                                                                 return_charges = True))
    def __calc_qt3_frags(self, mz_max, iso_range):
        iso_shift_max = int(min(iso_range, (mz_max - self.precursor_mz) * self.charge)) + 1
        self.qt3_frags = [self.precursor_mz + iso_shift / self.charge for iso_shift in range(iso_shift_max)]
    def __calc_lib_frags(self, frag_mz_list, frag_charge_list, frag_series_list, mz_min, mz_max):
        valid_fragment_indice = [i for i, frag in enumerate(frag_mz_list) if mz_min <= frag < mz_max]
        self.lib_frags = [frag_mz_list[i] for i in valid_fragment_indice]
        self.lib_frag_charges = [frag_charge_list[i] for i in valid_fragment_indice]
    def __calc_iso_frags(self, mz_min, mz_max):
        self.iso_frags = self.filter_frags([mz + 1 / c for mz, c in zip(self.lib_frags, self.lib_frag_charges)], 
                                           mz_min, mz_max, padding = True)
    def __calc_light_frags(self, mz_min, mz_max):
        self.light_frags = self.filter_frags([mz - 1 / c for mz, c in zip(self.lib_frags, self.lib_frag_charges)], 
                                             mz_min, mz_max, padding = True)
    def calc_frags(self, frag_mz_list, frag_charge_list, frag_series_list, mz_min, mz_max, iso_range):
        self.__calc_self_frags(mz_min, mz_max)
        self.__calc_qt3_frags(mz_max, iso_range)
        self.__calc_lib_frags(frag_mz_list, frag_charge_list, frag_series_list, mz_min, mz_max)
        self.__calc_iso_frags(mz_min, mz_max)
        self.__calc_light_frags(mz_min, mz_max)
def load_irt_precursors(irt_library, lib_cols, mz_min, mz_max, iso_range, n_threads):
    irt_precursors = []
    precursor_ids = list(np.unique(irt_library[lib_cols["PRECURSOR_ID_COL"]]))
    for precursor in precursor_ids:
        library_part = irt_library[irt_library[lib_cols["PRECURSOR_ID_COL"]] == precursor]
        precursor_obj = IRT_Precursor(list(library_part.loc[:, lib_cols["PRECURSOR_ID_COL"]])[0], 
                                      list(library_part.loc[:, lib_cols["FULL_SEQUENCE_COL"]])[0], 
                                      list(library_part.loc[:, lib_cols["PRECURSOR_CHARGE_COL"]])[0], 
                                      list(library_part.loc[:, lib_cols["PRECURSOR_MZ_COL"]])[0], 
                                      list(library_part.loc[:, lib_cols["IRT_COL"]])[0], 
                                      list(library_part.loc[:, lib_cols["PROTEIN_NAME_COL"]])[0])
        precursor_obj.calc_frags(list(library_part[lib_cols["FRAGMENT_MZ_COL"]]), 
                                 list(library_part[lib_cols["FRAGMENT_CHARGE_COL"]]),
                                 list(library_part[lib_cols["FRAGMENT_SERIES_COL"]]), 
                                 mz_min, mz_max, iso_range)
        irt_precursors.append(precursor_obj)
    n_precursors = len(irt_precursors)
    n_each_chunk = n_precursors // n_threads
    chunk_indice = [[k + i * n_each_chunk for k in range(n_each_chunk)] for i in range(n_threads)]
    for i, idx in enumerate(range(chunk_indice[-1][-1] + 1, n_precursors)):
        chunk_indice[i].append(idx)
    return irt_precursors, chunk_indice
def extract_irt_xics(ms1, ms2, win_range, extract_queue, precursor_list, 
                     model_cycles, mz_unit, mz_min, mz_max, mz_tol_ms1, mz_tol_ms2, iso_range, 
                     n_lib_frags, n_self_frags, n_qt3_frags, n_ms1_frags, n_iso_frags, n_light_frags, p_id):
    feature_dimension = n_lib_frags * 3 + n_self_frags + n_qt3_frags + n_ms1_frags + n_iso_frags + n_light_frags
    for idx, precursor in enumerate(precursor_list):
        precursor_win_id = calc_win_id(precursor.precursor_mz, win_range)
        lib_xics = np.array([calc_XIC(ms2[precursor_win_id].spectra, frag, mz_unit, mz_tol_ms2) for frag in precursor.lib_frags])
        lib_xics_1 = np.array([calc_XIC(ms2[precursor_win_id].spectra, frag, mz_unit, 0.2 * mz_tol_ms2) for frag in precursor.lib_frags])
        lib_xics_2 = np.array([calc_XIC(ms2[precursor_win_id].spectra, frag, mz_unit, 0.45 * mz_tol_ms2) for frag in precursor.lib_frags])
        self_xics = np.array([calc_XIC(ms2[precursor_win_id].spectra, frag, mz_unit, mz_tol_ms2) for frag in precursor.self_frags])
        qt3_xics = np.array([calc_XIC(ms2[precursor_win_id].spectra, frag, mz_unit, mz_tol_ms2) for frag in precursor.qt3_frags])
        ms1_xics = [calc_XIC(ms1.spectra, precursor.precursor_mz, mz_unit, mz_tol_ms1), 
                    calc_XIC(ms1.spectra, precursor.precursor_mz, mz_unit, 0.2 * mz_tol_ms1), 
                    calc_XIC(ms1.spectra, precursor.precursor_mz, mz_unit, 0.45 * mz_tol_ms1)]
        ms1_iso_frags = [precursor.precursor_mz - 1 / precursor.charge] + [precursor.precursor_mz + iso_shift / precursor.charge for iso_shift in range(1, iso_range + 1)]
        ms1_iso_frags = [i for i in ms1_iso_frags if mz_min <= i < mz_max]
        ms1_xics.extend([calc_XIC(ms1.spectra, frag, mz_unit, mz_tol_ms1) for frag in ms1_iso_frags])
        ms1_xics = np.array(ms1_xics)
        iso_xics = np.array([calc_XIC(ms2[precursor_win_id].spectra, frag, mz_unit, mz_tol_ms2) for frag in precursor.iso_frags])
        light_xics = np.array([calc_XIC(ms2[precursor_win_id].spectra, frag, mz_unit, mz_tol_ms2) for frag in precursor.light_frags])
        precursor_matrices, middle_rt_list = [], []
        for start_cycle in range(len(ms1.rt_list) - model_cycles + 1):
            end_cycle = start_cycle + model_cycles
            middle_rt_list.append(ms1.rt_list[start_cycle + model_cycles // 2])
            lib_matrix = lib_xics[:, start_cycle : end_cycle]
            lib_matrix_1 = lib_xics_1[:, start_cycle : end_cycle]
            lib_matrix_2 = lib_xics_2[:, start_cycle : end_cycle]
            self_matrix = self_xics[:, start_cycle : end_cycle]
            qt3_matrix = qt3_xics[:, start_cycle : end_cycle]
            ms1_matrix = ms1_xics[:, start_cycle : end_cycle]
            iso_matrix = iso_xics[:, start_cycle : end_cycle]
            light_matrix = light_xics[:, start_cycle : end_cycle]
            self_matrix = filter_matrix(self_matrix)
            qt3_matrix = filter_matrix(qt3_matrix)
            lib_matrix = tools.smooth_array(lib_matrix.astype(float))
            lib_matrix_1 = tools.smooth_array(lib_matrix_1.astype(float))
            lib_matrix_2 = tools.smooth_array(lib_matrix_2.astype(float))
            self_matrix = tools.smooth_array(self_matrix.astype(float))
            qt3_matrix = tools.smooth_array(qt3_matrix.astype(float))
            ms1_matrix = tools.smooth_array(ms1_matrix.astype(float))
            iso_matrix = tools.smooth_array(iso_matrix.astype(float))
            light_matrix = tools.smooth_array(light_matrix.astype(float))
            if lib_matrix.shape[0] > 0:
                std_indice, pearson_sums = calc_pearson_sums(lib_matrix)
                sort_order = np.argsort(-np.array(pearson_sums))
                lib_matrix = lib_matrix[sort_order, :]
                lib_matrix_1 = lib_matrix_1[sort_order, :]
                lib_matrix_2 = lib_matrix_2[sort_order, :]
                iso_matrix = iso_matrix[sort_order, :]
                light_matrix = light_matrix[sort_order, :]
                if self_matrix.shape[0] > 1 and len(std_indice) >= 1:
                    self_pearson = np.array([tools.calc_pearson(self_matrix[i, :], lib_matrix[0, :]) for i in range(self_matrix.shape[0])])
                    self_matrix = self_matrix[np.argsort(-self_pearson), :]
                if qt3_matrix.shape[0] > 1 and len(std_indice) >= 1:
                    qt3_pearson = np.array([tools.calc_pearson(qt3_matrix[i, :], lib_matrix[0, :]) for i in range(qt3_matrix.shape[0])])
                    qt3_matrix = qt3_matrix[np.argsort(-qt3_pearson), :]
            lib_matrix = adjust_size(lib_matrix, n_lib_frags)
            lib_matrix_1 = adjust_size(lib_matrix_1, n_lib_frags)
            lib_matrix_2 = adjust_size(lib_matrix_2, n_lib_frags)
            self_matrix = adjust_size(self_matrix, n_self_frags)
            qt3_matrix = adjust_size(qt3_matrix, n_qt3_frags)
            ms1_matrix = adjust_size(ms1_matrix, n_ms1_frags)
            iso_matrix = adjust_size(iso_matrix, n_iso_frags)
            light_matrix = adjust_size(light_matrix, n_light_frags)
            training_matrix = np.zeros((feature_dimension, model_cycles))
            part1_indice = (0, 
                            lib_matrix.shape[0])
            part2_indice = (n_lib_frags, 
                            n_lib_frags + self_matrix.shape[0])
            part3_indice = (n_lib_frags + n_self_frags, 
                            n_lib_frags + n_self_frags + qt3_matrix.shape[0])
            part4_indice = (n_lib_frags + n_self_frags + n_qt3_frags, 
                            n_lib_frags + n_self_frags + n_qt3_frags + ms1_matrix.shape[0])
            part5_indice = (n_lib_frags + n_self_frags + n_qt3_frags + n_ms1_frags, 
                            n_lib_frags + n_self_frags + n_qt3_frags + n_ms1_frags + iso_matrix.shape[0])
            part6_indice = (n_lib_frags + n_self_frags + n_qt3_frags + n_ms1_frags + n_iso_frags, 
                            n_lib_frags + n_self_frags + n_qt3_frags + n_ms1_frags + n_iso_frags + light_matrix.shape[0])
            part7_indice = (n_lib_frags + n_self_frags + n_qt3_frags + n_ms1_frags + n_iso_frags + n_light_frags, 
                            n_lib_frags + n_self_frags + n_qt3_frags + n_ms1_frags + n_iso_frags + n_light_frags + lib_matrix_1.shape[0])
            part8_indice = (n_lib_frags + n_self_frags + n_qt3_frags + n_ms1_frags + n_iso_frags + n_light_frags + n_lib_frags, 
                            n_lib_frags + n_self_frags + n_qt3_frags + n_ms1_frags + n_iso_frags + n_light_frags + n_lib_frags + lib_matrix_2.shape[0])

            if lib_matrix.shape[1] != model_cycles:
                lib_matrix = adjust_cycle(lib_matrix, model_cycles)
            if self_matrix.shape[1] != model_cycles:
                self_matrix = adjust_cycle(self_matrix, model_cycles)
            if qt3_matrix.shape[1] != model_cycles:
                qt3_matrix = adjust_cycle(qt3_matrix, model_cycles)
            if ms1_matrix.shape[1] != model_cycles:
                ms1_matrix = adjust_cycle(ms1_matrix, model_cycles)
            if iso_matrix.shape[1] != model_cycles:
                iso_matrix = adjust_cycle(iso_matrix, model_cycles)
            if light_matrix.shape[1] != model_cycles:
                light_matrix = adjust_cycle(light_matrix, model_cycles)
            if lib_matrix_1.shape[1] != model_cycles:
                lib_matrix_1 = adjust_cycle(lib_matrix_1, model_cycles)
            if lib_matrix_2.shape[1] != model_cycles:
                lib_matrix_2 = adjust_cycle(lib_matrix_2, model_cycles)

            training_matrix[part1_indice[0] : part1_indice[1], :] = lib_matrix
            training_matrix[part2_indice[0] : part2_indice[1], :] = self_matrix
            training_matrix[part3_indice[0] : part3_indice[1], :] = qt3_matrix
            training_matrix[part4_indice[0] : part4_indice[1], :] = ms1_matrix
            training_matrix[part5_indice[0] : part5_indice[1], :] = iso_matrix
            training_matrix[part6_indice[0] : part6_indice[1], :] = light_matrix
            training_matrix[part7_indice[0] : part7_indice[1], :] = lib_matrix_1
            training_matrix[part8_indice[0] : part8_indice[1], :] = lib_matrix_2
            training_matrix = training_matrix.T
            training_matrix = MinMaxScaler().fit_transform(training_matrix)
            precursor_matrices.append(training_matrix)
        extract_queue.put([precursor.iRT, middle_rt_list, np.array(precursor_matrices)])
    extract_queue.put(None)
def score_irt(extract_queue, BM_model_file, out_file_dir, n_threads, score_cutoff):
    BM_model = load_model(BM_model_file, compile = False)
    irt_recas, rt_no1 = [], []
    none_count = 0
    while True:
        irt_data = extract_queue.get()
        if irt_data is None:
            none_count += 1
            extract_queue.task_done()
            if none_count >= n_threads:
                break
            else:
                continue
        iRT, middle_rt_list, precursor_matrices = irt_data
        if precursor_matrices.shape[0] > 0:
            scores = BM_model(precursor_matrices, training = False)
            max_index = np.argmax(scores)
            if scores[max_index] >= score_cutoff:
                irt_recas.append(iRT)
                rt_no1.append(middle_rt_list[max_index])
        extract_queue.task_done()
    irt_pairs = [(irt, rt) for (irt, rt) in zip(irt_recas, rt_no1)]
    irt_pairs.sort(key = lambda x : x[0])
    if not os.path.exists(out_file_dir):
        os.mkdir(out_file_dir)
    with open(os.path.join(out_file_dir, "time_points.txt"), "w") as f:
        f.writelines("%.5f\t%.2f\n" % (irt, rt) for (irt, rt) in irt_pairs)
def fit_irt_model(out_file_dir, seed, rt_norm_model):
    irt_recas, rt_no1 = [], []
    f = open(os.path.join(out_file_dir, "time_points.txt"))
    for line in f:
        irt, rt = line.strip().split("\t")
        irt_recas.append(float(irt))
        rt_no1.append(float(rt))
    f.close()
    if rt_norm_model == "linear":
        lr_RAN = RANSACRegressor(LinearRegression(), random_state = seed)
        lr_RAN.fit(np.array(irt_recas).reshape(-1, 1), rt_no1)
        new_lr = LinearRegression()
        new_lr.fit(np.array(irt_recas).reshape(-1, 1)[lr_RAN.inlier_mask_], np.array(rt_no1)[lr_RAN.inlier_mask_])
        r2 = new_lr.score(np.array(irt_recas).reshape(-1, 1)[lr_RAN.inlier_mask_], np.array(rt_no1)[lr_RAN.inlier_mask_])
        slope, intercept = new_lr.coef_[0], new_lr.intercept_
        with open(os.path.join(out_file_dir, "linear_irt_model.txt"), "w") as f:
            f.write("%s\n" % slope)
            f.write("%s\n" % intercept)
            f.write("%s\n" % r2)
        line_X = np.arange(min(irt_recas) - 2, max(irt_recas) + 2)
        line_y = new_lr.predict(line_X[:, np.newaxis])
        plt.figure(figsize = (6, 6))
        plt.scatter(irt_recas, rt_no1)
        plt.plot(line_X, line_y)
        plt.xlabel("iRT")
        plt.ylabel("RT by DreamDIA-XMBD")
        plt.title("DreamDIA-XMBD RT normalization, $R^2 = $%.5f" % r2)
        plt.savefig(os.path.join(out_file_dir, "irt_model.pdf"))
        return [slope, intercept]
    else:
        smoothed_points = sm.nonparametric.lowess(np.array(rt_no1), np.array(irt_recas), frac = 0.2, it = 3, delta = 0)
        nl_model_parameters = np.polyfit(smoothed_points[:, 0], smoothed_points[:, 1], 9)
        nl_model = np.poly1d(nl_model_parameters)
        with open(os.path.join(out_file_dir, "nonlinear_irt_model.txt"), "w") as f:
            f.write("\n".join([str(each) for each in nl_model_parameters]))
            f.write("\n")       
        line_X = np.arange(min(irt_recas) - 2, max(irt_recas) + 2)
        line_y = nl_model(line_X)
        plt.figure(figsize = (6, 6))
        plt.scatter(irt_recas, rt_no1)
        plt.plot(line_X, line_y, c = "red")
        plt.xlabel("iRT")
        plt.ylabel("RT by DreamDIA-XMBD")
        plt.title("DreamDIA-XMBD RT normalization, Non-linear")
        plt.savefig(os.path.join(out_file_dir, "irt_model.pdf"))
        return nl_model_parameters
