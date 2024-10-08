import json,csv
import pandas as pd
import os
import difflib
import pickle
import re

def process_raw_data(filter_csv_path, raw_csv_path):
    if os.path.exists(filter_csv_path):
        pd_filter = pd.read_csv(filter_csv_path)
        # pd_filter = pd.read_csv('/home/MSR_data_cleaned_pairs_Test.csv')

    else:
        pd_raw = pd.read_csv(raw_csv_path)
        filter_csv = pd_raw.loc[pd_raw["vul"] == 1]
        filter_csv.to_csv(filter_csv_path)
        pd_filter = pd.read_csv(filter_csv_path)
    return pd_filter

def label(f_vul, f_novul ,label_dict, outfile):
    diff = list(difflib.unified_diff(f_novul.splitlines(), f_vul.splitlines()))
    split_list = [i for i,line in enumerate(diff) if line.startswith("@@")]
    split_list.append(len(diff))
    i = 0
    for i in range(len(split_list) - 1):
        start = split_list[i]
        del_linenum = diff[start].split("@@ -")[-1].split(",")[0].split('+')[-1].strip()
        end = split_list[i + 1]
        
        line_num = int(del_linenum)
        for line in diff[start+1 : end]:
            if line.startswith("-"):
                label_dict[outfile].append(line_num)
            elif line.startswith("+"):
                line_num -= 1
            line_num += 1
        i += 1

def get_diff(before, after):
    text1 = before.splitlines(keepends=True)
    text2 = after.splitlines(keepends=True)
    diff = difflib.context_diff(text1, text2, n=0)
    ret = []
    llm, rlm = 0, -1
    
    for i, line in enumerate(diff):
        if llm > rlm:
            line_pattern = r'([\*-]{3})\s*(\d+(?:,\d+)*)\s*[\*-]{4}'
            line_match = re.search(line_pattern, line)
            if line_match is not None:
                signal = line_match.group(1)
                lm = line_match.group(2)
                if ',' in lm:
                    splited = lm.split(',')
                    llm = int(splited[0])
                    rlm = int(splited[1])
                else:
                    llm = rlm = int(lm)
                continue
        
        edit_pattern = r'^([-!](?![-!]))(.+)'
        edit_match = re.search(edit_pattern, line)
        if edit_match is None:
            continue
        op = edit_match.group(1)
        code = edit_match.group(2)
        split_word = '@yuanshen@'
        if (signal == '***' and op == '!') or (signal == '---' and op == '-'):
            ret.append(split_word.join([str(llm), op, code]))
        llm += 1
        
    #ret = "\n".join(ret)
    return ret

def main():
    dataset_path = '/home/dataset/'
    raw_data_path = 'raw_data/'
    raw_data_filename = 'MSR_data_cleaned.csv'
    filter_data_filename = 'MSR_data_filtered.csv'

    dataset_path_output = 'dataset_test/'
    label_pkl_file = 'test_label_pkl.pkl'
    
    #filter_csv_path = dataset_path + raw_data_path + filter_data_filename
    filter_csv_path = '/home/MSR_data_cleaned_pairs.csv'
    #raw_csv_path = dataset_path + raw_data_path + raw_data_filename
    raw_csv_path = '/home/MSR_data_cleaned.csv'
    #output_path = 'dataset_path + dataset_path_output
    out_path_before = '/home/VulGnnExp/All_code/1_vul'
    out_path_after = '/home/VulGnnExp/All_code/0_novul'
    out_path_patch = '/home/VulGnnExp/Src_code/patch'
    #pkl_path = dataset_path + label_pkl_file
    json_path = '/home/VulGnnExp/nvd_vul_label.json'

    pd_filter = process_raw_data(filter_csv_path, raw_csv_path)
    file_cnt = pd_filter.shape[0]
    file_num = 0
    label_dict = {}
    cnt_1 = 0

    for index, row in pd_filter.iterrows():
    
        file_num += 1
        print(str(file_cnt) + ' / ' + str(file_num))
        cve_id = row['CVE ID']
        cwe_id = row['CWE ID']
        project_name = row["project"]
        hash_vaule = row['commit_id']
        flag_W=0
        try:
            file_name = cve_id + "_" + project_name + "_" + cwe_id + "_" + hash_vaule
        except:
            #continue
            flag_W=1
            try:
                file_name = cve_id + "_" + project_name + "_"  + hash_vaule
            except:
                continue
        # 0_CVE-2015-8467_samba_CWE-264_b000da128b5fb519d2d3f2e7fd20e4a25b7dae7d
        #outfile = output_path + file_name
        outfile = file_name

        file_name_cnt = 1
        outfile_new = outfile
        while outfile_new in label_dict.keys():
            outfile_new = outfile + '_' + str(file_name_cnt)
            file_name_cnt += 1
        label_dict[outfile_new] = []
        #outfile_new = outfile_new+'.c'
        # if not os.path.exists(outfile_new):
        #     os.mkdir(outfile_new)


        func_before = row['func_before']
        func_after = row["func_after"]
        func_patch = row["patch"]
        vul_file_name = '1_'+ outfile_new
        novul_file_name = '0_' + outfile_new

        # if flag_W==1:
        #     with open(out_path_before + '/'+ outfile_new+'.c', 'w', encoding='utf-8') as f_vul:
        #         f_vul.write(func_before)
        #         cnt_1 += 1

        #     with open(out_path_patch + '/'+ outfile_new+'.c', 'w', encoding='utf-8') as f_vul:
        #         f_vul.write(func_patch)
        #         cnt_1 += 1

        #     with open(out_path_after + '/' + outfile_new+'.c', 'w', encoding='utf-8') as f_novul:
        #         f_novul.write(func_after)
        #         cnt_1 += 1

        #if pd.isnull(row['lines_before']):
        #    label_dict[outfile_new] = ['']
        #else:
        rets = get_diff(func_before, func_after)
        if rets != '':
            for ret in rets:
                label_dict[outfile_new].append(int(ret.split('@yuanshen@')[0]))


    #with open(pkl_path,'wb') as f:
    #    pickle.dump(label_dict, f)
    with open(json_path,'w') as f:
        json.dump(label_dict, f)

    print(cnt_1/3)


if __name__ == '__main__':
    main()