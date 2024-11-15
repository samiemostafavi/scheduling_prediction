import os, sys, gzip, json, sqlite3, random, pickle
import plotly.graph_objects as go
import numpy as np
import pandas as pd
from pathlib import Path
from loguru import logger
import plotly.graph_objects as go

from edaf.core.uplink.preprocess import preprocess_ul
from edaf.core.uplink.analyze_channel import ULChannelAnalyzer
from edaf.core.uplink.analyze_packet import ULPacketAnalyzer
from edaf.core.uplink.analyze_scheduling import ULSchedulingAnalyzer
from plotly.subplots import make_subplots

if not os.getenv('DEBUG'):
    logger.remove()
    logger.add(sys.stdout, level="INFO")

# in case you have offline parquet journey files, you can use this script to decompose delay
# pass the address of a folder in argv with the following structure:
# FOLDER_ADDR/
# -- gnb/
# ---- latseq.*.lseq
# -- ue/
# ---- latseq.*.lseq
# -- upf/
# ---- se_*.json.gz

# create database file by running
# python preprocess.py results/240928_082545_results

# it will result a database.db file inside the given directory

def preprocess_edaf(args):

    folder_path = Path(args.source)
    result_database_file = folder_path / 'database.db'

    # GNB
    gnb_path = folder_path.joinpath("gnb")
    gnb_lseq_file = list(gnb_path.glob("*.lseq"))[0]
    logger.info(f"found gnb lseq file: {gnb_lseq_file}")
    gnb_lseq_file = open(gnb_lseq_file, 'r')
    gnb_lines = gnb_lseq_file.readlines()
    
    # UE
    ue_path = folder_path.joinpath("ue")
    ue_lseq_file = list(ue_path.glob("*.lseq"))[0]
    logger.info(f"found ue lseq file: {ue_lseq_file}")
    ue_lseq_file = open(ue_lseq_file, 'r')
    ue_lines = ue_lseq_file.readlines()

    # NLMT
    nlmt_path = folder_path.joinpath("upf")
    nlmt_file = list(nlmt_path.glob("se_*"))[0]
    if nlmt_file.suffix == '.json':
        with open(nlmt_file, 'r') as file:
            nlmt_records = json.load(file)['oneway_trips']
    elif nlmt_file.suffix == '.gz':
        with gzip.open(nlmt_file, 'rt', encoding='utf-8') as file:
            nlmt_records = json.load(file)['oneway_trips']
    else:
        logger.error(f"NLMT file format not supported: {nlmt_file.suffix}")
    logger.info(f"found nlmt file: {nlmt_file}")

    # Open a connection to the SQLite database
    conn = sqlite3.connect(result_database_file)
    # process the lines
    preprocess_ul(conn, gnb_lines, ue_lines, nlmt_records)
    # Close the connection when done
    conn.close()
    logger.success(f"Tables successfully saved to '{result_database_file}'.")


def find_closest_schedule(failed_ul_schedules, ts_value, hapid_value):
    
    # Filter items by hqpid
    closest_item = None
    closest_index = -1
    min_diff = float('inf')

    for index, item in enumerate(failed_ul_schedules):
        if item.get('sched.cause.hqpid') == hapid_value:
            timestamp = item.get('ue_scheduled_ts')
            if timestamp < ts_value:
                diff = ts_value - timestamp
                if diff < min_diff and diff < 0.05:
                    min_diff = diff
                    closest_item = item
                    closest_index = index
    
    return closest_item, closest_index


# here we process two general types of events:

# 1) packet arrival event that includes the following:
# - MCS index (of the first segment)
# - Number of harq retransmissions (sum of harq retransmissions of all rlc segments)
# - Number of rlc retransmissions (total number of rlc retransmissions)
# - Packet size
# - Number of resource blocks = 0
# - Number of symbols = 0
# 2) scheduling events of a packet
# - MCS index
# - Number of harq retransmissions
# - RLC retransmission needed
# - transport block size
# - Number of resource blocks
# - Number of symbols

# the plan is to predict the intensity of sheduling events, given the packet arrival event plus the MCS, number of retransmissions, and its size

# in order to make the model distinguish between the packets and their schedules, we assign more event types:
# [ ar5 seg4 seg4 seg4 seg4 ar3 seg2 seg2 ar1 seg2 seg0 seg0 seg0 seg0 ]
# therefore, total number of event types would be the number of packet arrivals in the window times 2.

# and then the model's job is to predict the intensity of seg0, just the intensity, not the MCS or retransmissions etc.

def plot_scheduling_data(args):

    # read configuration from args.config
    with open(args.config, 'r') as f:
        config = json.load(f)
    # select the source configuration
    config = config[args.configname]

    # read experiment configuration
    folder_addr = Path(args.source)
    # find all .db files in the folder
    db_files = list(folder_addr.glob("*.db"))
    if not db_files:
        logger.error("No database files found in the specified folder.")
        return
    result_database_files = [str(db_file) for db_file in db_files]

    # read exp configuration from args.config
    with open(folder_addr / 'experiment_config.json', 'r') as f:
        exp_config = json.load(f)

    time_masks = config['time_masks']
    filter_packet_sizes = config['filter_packet_sizes']
    window_config = config['window_config']
    dataset_size_max = config['dataset_size_max']
    split_ratios = config['split_ratios']
    dtime_max = config['dtime_max']
    
    slots_duration_ms = exp_config['slots_duration_ms']
    num_slots_per_frame = exp_config['slots_per_frame']
    total_prbs_num = exp_config['total_prbs_num']
    symbols_per_slot = exp_config['symbols_per_slot']
    scheduling_map_num_integers = exp_config['scheduling_map_num_integers']
    max_num_frames = exp_config['max_num_frames']
    scheduling_time_ahead_ms = exp_config['scheduling_time_ahead_ms']
    max_harq_attempts = exp_config['max_harq_attempts']

    # prepare the results folder
    results_folder_addr = folder_addr / 'scheduling_plots' / args.name
    results_folder_addr.mkdir(parents=True, exist_ok=True)
    with open(results_folder_addr / 'config.json', 'w') as f:
        json_obj = json.dumps(config, indent=4)
        f.write(json_obj)

    # common
    arrivals_ts_list, arrivals_size_list = np.array([]), np.array([])
    mcs_val_list, mcs_ts_list = np.array([]), np.array([])
    repeated_ue_rlc_val_list, repeated_ue_rlc_ts_list = np.array([]), np.array([])
    ue_ndi0_mac_val_list, ue_ndi0_mac_text_list, ue_ndi0_mac_ts_list = np.array([]), np.array([]), np.array([])

    # non fast mode
    packet_len_list, packet_mrtx_list, packet_rrtx_list, packet_mcs_list, packet_ts_list = np.array([]), np.array([]), np.array([]), np.array([]), np.array([])
    segment_len_list, segment_mrtx_list, segment_rrtx_list, segment_mcs_list, segment_ts_list = np.array([]), np.array([]), np.array([]), np.array([]), np.array([])

    prev_end_ts = 0
    for result_database_file, time_mask in zip(result_database_files, time_masks):
        # initiate the analyzers
        chan_analyzer = ULChannelAnalyzer(result_database_file)
        packet_analyzer = ULPacketAnalyzer(result_database_file)
        sched_analyzer = ULSchedulingAnalyzer(
            total_prbs_num = total_prbs_num, 
            symbols_per_slot = symbols_per_slot,
            slots_per_frame = num_slots_per_frame, 
            slots_duration_ms = slots_duration_ms, 
            scheduling_map_num_integers = scheduling_map_num_integers,
            max_num_frames = max_num_frames,
            db_addr = result_database_file
        )
        experiment_length_ts = packet_analyzer.last_ueip_ts - packet_analyzer.first_ueip_ts
        logger.info(f"Total experiment duration: {(experiment_length_ts)} seconds")

        begin_ts = packet_analyzer.first_ueip_ts+experiment_length_ts*time_mask[0]
        end_ts = packet_analyzer.first_ueip_ts+experiment_length_ts*time_mask[1]
        logger.info(f"Filtering packet arrival events from {begin_ts} to {end_ts}, duration: {experiment_length_ts*time_mask[1]-experiment_length_ts*time_mask[0]} seconds")

        # find the packet arrivals
        packet_arrivals = packet_analyzer.figure_packet_arrivals_from_ts(begin_ts, end_ts)
        logger.info(f"Number of packet arrivals for this duration: {len(packet_arrivals)}")
        arrivals_size_list = np.concatenate((arrivals_size_list, np.array([item['ip.in.length'] for item in packet_arrivals])))
        arrivals_ts_list = np.concatenate((arrivals_ts_list, np.array([(item['ip.in.timestamp']-begin_ts+prev_end_ts)*1000 for item in packet_arrivals])))

        # analyze packets
        packets = packet_analyzer.figure_packettx_from_ts(begin_ts, begin_ts+0.01)
        packets_rnti_set = set([item['rlc.attempts'][0]['rnti'] for item in packets])
        # remove None from the set
        packets_rnti_set.discard(None)
        logger.info(f"RNTIs in the packet stream: {packets_rnti_set}")
        if len(packets_rnti_set) > 1:
            logger.error("Multiple RNTIs in the packet stream, exiting...")
            return
        stream_rnti = list(packets_rnti_set)[0]

        # extract MCS value time series
        mcs_arr = chan_analyzer.find_mcs_from_ts(begin_ts,end_ts)
        set_rnti = set([item['rnti'] for item in mcs_arr])
        logger.info(f"Number of unique RNTIs in MCS indices: {len(set_rnti)}")
        # filter out the MCS values for the stream RNTI
        mcs_val_list = np.concatenate((mcs_val_list, np.array([item['mcs'] for item in mcs_arr if item['rnti'] == stream_rnti])))
        mcs_ts_list = np.concatenate((mcs_ts_list, np.array([(item['timestamp']-begin_ts+prev_end_ts)*1000 for item in mcs_arr if item['rnti'] == stream_rnti])))

        # find repeated RLC attempts
        repeated_ue_rlc_attempts = chan_analyzer.find_repeated_ue_rlc_attempts_from_ts(begin_ts, end_ts)
        repeated_ue_rlc_val_list = np.concatenate((repeated_ue_rlc_val_list, np.array([0 for _ in repeated_ue_rlc_attempts])))
        repeated_ue_rlc_ts_list = np.concatenate((repeated_ue_rlc_ts_list, np.array([(item['rlc.txpdu.timestamp']-begin_ts+prev_end_ts)*1000 for item in repeated_ue_rlc_attempts])))

        # find MAC attempts with ndi=0 (NACKs basically)
        ue_ndi0_mac_attempts = chan_analyzer.find_ndi0_ue_mac_attempts_from_ts(begin_ts, end_ts)
        ue_ndi0_mac_val_list = np.concatenate((ue_ndi0_mac_val_list, np.array([item['phy.tx.real_rvi'] for item in ue_ndi0_mac_attempts])))
        ue_ndi0_mac_text_list = np.concatenate((ue_ndi0_mac_text_list, np.array([item['mac.harq.hqpid'] for item in ue_ndi0_mac_attempts])))
        ue_ndi0_mac_ts_list = np.concatenate((ue_ndi0_mac_ts_list, np.array([(item['phy.tx.timestamp']-begin_ts+prev_end_ts)*1000 for item in ue_ndi0_mac_attempts])))

        if not args.fast:

            # analyze packets
            packets = packet_analyzer.figure_packettx_from_ts(begin_ts, end_ts)
            packets_rnti_set = set([item['rlc.attempts'][0]['rnti'] for item in packets])
            # remove None from the set
            packets_rnti_set.discard(None)
            logger.info(f"RNTIs in the packet stream: {packets_rnti_set}")
            if len(packets_rnti_set) > 1:
                logger.error("Multiple RNTIs in the packet stream, exiting...")
                return
            stream_rnti = list(packets_rnti_set)[0]

            this_db_events = []
            logger.info(f"Extract events for plotting")
            for idx, packet in enumerate(packets):
                print(f"\rProcessing packet {idx + 1}/{len(packets)} ({(idx + 1) / len(packets) * 100:.2f}%) with packet sn: {packet['sn']}", end="")
                this_db_events.append(
                    {
                        'packet_or_segment' : True,
                        'packet_id' : idx,
                        'timestamp' : packet['ip.in_t'],
                        'len' : packet['len'],
                        'mcs_index' : packet['rlc.attempts'][0]['mac.attempts'][0]['mcs'],
                        'mac_retx' : sum([len(rlc_attempt['mac.attempts'])-1 for rlc_attempt in packet['rlc.attempts'] if not rlc_attempt['repeated']]),
                        'rlc_failed' : sum([1 for rlc_attempt in packet['rlc.attempts'] if not rlc_attempt['acked']]),
                    }
                )
                for rlc_attempt in packet['rlc.attempts']:
                    this_db_events.append(
                        {
                            'packet_or_segment' : False,
                            'packet_id' : idx,
                            'timestamp' : rlc_attempt['mac.in_t'],
                            'len' : rlc_attempt['len'],
                            'mcs_index' : rlc_attempt['mac.attempts'][0]['mcs'],
                            'mac_retx' : len(rlc_attempt['mac.attempts'])-1,
                            'rlc_failed' : int(not rlc_attempt['acked']),
                        }
                    )
            print("\n", end="")

            # packets time series
            packet_len_list = np.concatenate((packet_len_list,np.array([event['len'] for event in this_db_events if event['packet_or_segment']])))
            packet_mrtx_list = np.concatenate((packet_mrtx_list,np.array([event['mac_retx'] for event in this_db_events if event['packet_or_segment']])))
            packet_rrtx_list = np.concatenate((packet_rrtx_list,np.array([event['rlc_failed'] for event in this_db_events if event['packet_or_segment']])))
            packet_mcs_list = np.concatenate((packet_mcs_list,np.array([event['mcs_index'] for event in this_db_events if event['packet_or_segment']])))
            packet_ts_list = np.concatenate((packet_ts_list,np.array([(event['timestamp']-begin_ts+prev_end_ts)*1000 for event in this_db_events if event['packet_or_segment']])))

            # segments time series
            segment_len_list = np.concatenate((segment_len_list,np.array([event['len'] for event in this_db_events if not event['packet_or_segment']])))
            segment_mrtx_list = np.concatenate((segment_mrtx_list,np.array([event['mac_retx'] for event in this_db_events if not event['packet_or_segment']])))
            segment_rrtx_list = np.concatenate((segment_rrtx_list,np.array([event['rlc_failed'] for event in this_db_events if not event['packet_or_segment']])))
            segment_mcs_list = np.concatenate((segment_mcs_list,np.array([event['mcs_index'] for event in this_db_events if not event['packet_or_segment']])))
            segment_ts_list = np.concatenate((segment_ts_list,np.array([(event['timestamp']-begin_ts+prev_end_ts)*1000 for event in this_db_events if not event['packet_or_segment']])))
                                             
        prev_end_ts = (end_ts-begin_ts) + prev_end_ts

    if args.fast:
        # Create a subplot figure with 2 rows
        fig = make_subplots(rows=2, cols=1, subplot_titles=('MCS Index', 'Packet arrivals'))
        fig.add_trace(go.Scatter(x=mcs_ts_list, y=mcs_val_list, mode='lines+markers', name='MCS value', marker=dict(symbol='circle')), row=1, col=1)
        fig.add_trace(go.Scatter(x=arrivals_ts_list, y=arrivals_size_list, mode='markers', name='Packet arrivals', marker=dict(symbol='square')), row=2, col=1)

        # for failed_ue_rlc attempts:
        fig.add_trace(go.Scatter(x=repeated_ue_rlc_ts_list, y=repeated_ue_rlc_val_list-0.5, mode='markers', name='Repeated RLC attempts', marker=dict(symbol='triangle-down')), row=1, col=1)
        
        # for ue_ndi0_mac_val_list:
        fig.add_trace(go.Scatter(x=ue_ndi0_mac_ts_list, y=ue_ndi0_mac_val_list-0.3, mode='markers+text', name='Ue mac ndi0', marker=dict(symbol='triangle-up'), text=ue_ndi0_mac_text_list, textposition='top center'), row=1, col=1)

        fig.update_layout(
            title='Link Data Plots',
            xaxis_title='Time [ms]',
            yaxis_title='Values',
            legend_title='Legend',
        )
        fig.update_xaxes(title_text='Time [ms]', row=1, col=1)
        fig.update_yaxes(title_text='Values', row=1, col=1)
        fig.update_xaxes(title_text='Time [ms]', row=2, col=1)
        fig.update_yaxes(title_text='Values', row=2, col=1)
        fig.update_xaxes(matches='x')
        fig.write_html(str(results_folder_addr / 'fast_plot.html'))
    else:

        # Create a subplot figure with 2 rows
        fig = make_subplots(rows=2, cols=1, subplot_titles=('MCS Index and Link Quality', 'Processed Events'))

        # MCS Index and Link Quality
        fig.add_trace(go.Scatter(x=mcs_ts_list, y=mcs_val_list, mode='lines+markers', name='MCS value', marker=dict(symbol='circle')), row=1, col=1)
        # for failed_ue_rlc attempts:
        fig.add_trace(go.Scatter(x=repeated_ue_rlc_ts_list, y=repeated_ue_rlc_val_list-0.5, mode='markers', name='Repeated RLC attempts', marker=dict(symbol='triangle-down')), row=1, col=1)    
        # for ue_ndi0_mac_val_list:
        fig.add_trace(go.Scatter(x=ue_ndi0_mac_ts_list, y=ue_ndi0_mac_val_list-0.3, mode='markers+text', name='Ue mac ndi0', marker=dict(symbol='triangle-up'), text=ue_ndi0_mac_text_list, textposition='top center'), row=1, col=1)

        # Processed Events
        fig.add_trace(go.Scatter(x=packet_ts_list, y=np.ones(len(packet_ts_list)), mode='markers+text', name='Packet arrivals', marker=dict(symbol='square'), text=packet_rrtx_list, textposition='top center'), row=2, col=1)
        fig.add_trace(go.Scatter(x=packet_ts_list, y=np.ones(len(packet_ts_list)), mode='markers+text', name='Packet arrivals', marker=dict(symbol='square'), text=packet_mrtx_list, textposition='bottom center'), row=2, col=1)
        fig.add_trace(go.Scatter(x=segment_ts_list, y=np.ones(len(segment_ts_list)), mode='markers+text', name='Segment events', marker=dict(symbol='circle'), text=segment_rrtx_list, textposition='top center'), row=2, col=1)
        fig.add_trace(go.Scatter(x=segment_ts_list, y=np.ones(len(segment_ts_list)), mode='markers+text', name='Segment events', marker=dict(symbol='circle'), text=segment_mrtx_list, textposition='bottom center'), row=2, col=1)
        fig.add_trace(go.Scatter(x=segment_ts_list, y=segment_len_list, mode='markers', name='Segment events lengths', marker=dict(symbol='circle')), row=2, col=1)

        fig.update_layout(
            title='Link and Scheduling Data Plots',
            xaxis_title='Time [ms]',
            yaxis_title='Values',
            legend_title='Legend',
        )
        fig.update_xaxes(title_text='Time [ms]', row=1, col=1)
        fig.update_yaxes(title_text='Values', row=1, col=1)
        fig.update_xaxes(title_text='Time [ms]', row=2, col=1)
        fig.update_yaxes(title_text='Values', row=2, col=1)
        fig.update_xaxes(matches='x')
        fig.write_html(str(results_folder_addr / 'combined_plot.html'))

    
def create_training_dataset(args):
    """
    Create a training dataset
    """

    # read configuration from args.config
    with open(args.config, 'r') as f:
        config = json.load(f)
    # select the source configuration
    config = config[args.configname]

    # read experiment configuration
    folder_addr = Path(args.source)
    # find all .db files in the folder
    db_files = list(folder_addr.glob("*.db"))
    if not db_files:
        logger.error("No database files found in the specified folder.")
        return
    result_database_files = [str(db_file) for db_file in db_files]

    # read exp configuration from args.config
    with open(folder_addr / 'experiment_config.json', 'r') as f:
        exp_config = json.load(f)

    time_masks = config['time_masks']
    filter_packet_sizes = config['filter_packet_sizes']

    # select the source configuration
    window_config = config['window_config']
    if window_config['type'] == 'event':
        window_size_events = window_config['size']
        max_num_packet_types = window_config['max_num_packet_types']
        dim_process = max_num_packet_types*2
    else:
        logger.error("Only event window configuration is supported for now.")
        return
    dataset_size_max = config['dataset_size_max']
    split_ratios = config['split_ratios']
    dtime_max = config['dtime_max']
    
    slots_duration_ms = exp_config['slots_duration_ms']
    num_slots_per_frame = exp_config['slots_per_frame']
    total_prbs_num = exp_config['total_prbs_num']
    symbols_per_slot = exp_config['symbols_per_slot']
    scheduling_map_num_integers = exp_config['scheduling_map_num_integers']
    max_num_frames = exp_config['max_num_frames']
    scheduling_time_ahead_ms = exp_config['scheduling_time_ahead_ms']
    max_harq_attempts = exp_config['max_harq_attempts']

    # prepare the results folder
    results_folder_addr = folder_addr / 'training_datasets' / args.name
    results_folder_addr.mkdir(parents=True, exist_ok=True)
    with open(results_folder_addr / 'config.json', 'w') as f:
        json_obj = json.dumps(config, indent=4)
        f.write(json_obj)

    # create prefinal list of events
    dataset = []
    for result_database_file, time_mask in zip(result_database_files, time_masks):
        # initiate the analyzers
        chan_analyzer = ULChannelAnalyzer(result_database_file)
        packet_analyzer = ULPacketAnalyzer(result_database_file)
        sched_analyzer = ULSchedulingAnalyzer(
            total_prbs_num = total_prbs_num, 
            symbols_per_slot = symbols_per_slot,
            slots_per_frame = num_slots_per_frame, 
            slots_duration_ms = slots_duration_ms, 
            scheduling_map_num_integers = scheduling_map_num_integers,
            max_num_frames = max_num_frames,
            db_addr = result_database_file
        )
        experiment_length_ts = packet_analyzer.last_ueip_ts - packet_analyzer.first_ueip_ts
        logger.info(f"Total experiment duration: {(experiment_length_ts)} seconds")

        begin_ts = packet_analyzer.first_ueip_ts+experiment_length_ts*time_mask[0]
        end_ts = packet_analyzer.first_ueip_ts+experiment_length_ts*time_mask[1]
        logger.info(f"Filtering packet arrival events from {begin_ts} to {end_ts}, duration: {experiment_length_ts*time_mask[1]-experiment_length_ts*time_mask[0]} seconds")

        # analyze packets
        packets = packet_analyzer.figure_packettx_from_ts(begin_ts, begin_ts+0.1)
        packets_rnti_set = set([item['rlc.attempts'][0]['rnti'] for item in packets])
        # remove None from the set
        packets_rnti_set.discard(None)
        logger.info(f"RNTIs in the packet stream: {packets_rnti_set}")
        if len(packets_rnti_set) > 1:
            logger.error("Multiple RNTIs in the packet stream, exiting...")
            return
        stream_rnti = list(packets_rnti_set)[0]

        # analyze packets
        packets = packet_analyzer.figure_packettx_from_ts(begin_ts, end_ts)
        packets_rnti_set = set([item['rlc.attempts'][0]['rnti'] for item in packets])
        # remove None from the set
        packets_rnti_set.discard(None)
        logger.info(f"RNTIs in the packet stream: {packets_rnti_set}")
        if len(packets_rnti_set) > 1:
            logger.error("Multiple RNTIs in the packet stream, exiting...")
            return
        stream_rnti = list(packets_rnti_set)[0]

        this_db_events_v1 = []
        logger.info(f"Extract events for dataset v1")
        for idx, packet in enumerate(packets):
            print(f"\rProcessing packet {idx + 1}/{len(packets)} ({(idx + 1) / len(packets) * 100:.2f}%) with packet sn: {packet['sn']}", end="")
            this_db_events_v1.append(
                {
                    'packet_or_segment' : True,
                    'packet_id' : idx,
                    'timestamp' : packet['ip.in_t'],
                    'len' : packet['len'],
                    'mcs_index' : packet['rlc.attempts'][0]['mac.attempts'][0]['mcs'],
                    'mac_retx' : sum([len(rlc_attempt['mac.attempts'])-1 for rlc_attempt in packet['rlc.attempts'] if not rlc_attempt['repeated']]),
                    'rlc_failed' : sum([1 for rlc_attempt in packet['rlc.attempts'] if not rlc_attempt['acked']]),
                    'num_rbs' : 0,
                    'num_symbols' : 0,
                }
            )
            for rlc_attempt in packet['rlc.attempts']:
                this_db_events_v1.append(
                    {
                        'packet_or_segment' : False,
                        'packet_id' : idx,
                        'timestamp' : rlc_attempt['mac.in_t'],
                        'len' : rlc_attempt['len'],
                        'mcs_index' : rlc_attempt['mac.attempts'][0]['mcs'],
                        'mac_retx' : len(rlc_attempt['mac.attempts'])-1,
                        'rlc_failed' : int(not rlc_attempt['acked']),
                        'num_rbs' : rlc_attempt['mac.attempts'][0]['rbs'],
                        'num_symbols' : rlc_attempt['mac.attempts'][0]['symbols'],
                    }
                )
        print("\n", end="")

        # sort the events based on timestamp
        this_db_events_v1 = sorted(this_db_events_v1, key=lambda x: x['timestamp'], reverse=False)

        # add timestamps relative to the frame0
        this_db_events_v2 = []
        last_mcs_event_ts = 0
        logger.info(f"Extract events for dataset v2")
        for idx, item in enumerate(this_db_events_v1):
            print(f"\rProcessing packet {idx + 1}/{len(this_db_events_v1)} ({(idx + 1) / len(this_db_events_v1) * 100:.2f}%) with packet id: {item['packet_id']}", end="")
            frame_start_ts, frame_num, slot_num = sched_analyzer.find_frame_slot_from_ts(
                timestamp=item['timestamp'],
                SCHED_OFFSET_S=scheduling_time_ahead_ms/1000 # 4ms which is 8*slot_duration_ms
            )

            time_since_frame0 = frame_num*num_slots_per_frame*slots_duration_ms + slot_num*slots_duration_ms
            time_since_last_event = time_since_frame0-last_mcs_event_ts
            #if time_since_last_event < 0:
            #    time_since_last_event = time_since_frame0 + max_num_frames*num_slots_per_frame*slots_duration_ms
            if time_since_last_event < 0:
                time_since_last_event = time_since_frame0

            last_mcs_event_ts = time_since_frame0

            if time_since_last_event > dtime_max:
                continue

            this_db_events_v2.append(
                {
                    **item,
                    'time_since_start' : time_since_frame0,
                    'time_since_last_event' : time_since_last_event,
                }
            )
        print("\n", end="")


        logger.info(f"Creating training dataset for this db final")
        this_db_dataset = create_training_dataset_event_window(this_db_events_v2, dim_process, window_size_events, max_num_packet_types, config)

        # print length of dataset
        logger.info(f"Number of total entries produced by this db dataset: {len(this_db_dataset)}")
        print(this_db_dataset[0])

        # append elements of one_db_dataset to dataset
        dataset.extend(this_db_dataset)

    # shuffle the dataset
    random.shuffle(dataset)

    logger.success(f"Number of total entries in the dataset: {len(dataset)}")

    # split
    train_num = int(len(dataset)*split_ratios[0])
    dev_num = int(len(dataset)*split_ratios[1])
    print("train: ", train_num, " - dev: ", dev_num)
    # train
    train_ds = {
        'dim_process' : int(dim_process),
        'train' : dataset[0:train_num],
    }


    # Save the dictionary to a pickle file
    with open(results_folder_addr / 'train.pkl', 'wb') as f:
        pickle.dump(train_ds, f)
    # dev
    dev_ds = {
        'dim_process' : dim_process,
        'dev' : dataset[train_num:train_num+dev_num],
    }

    # Save the dictionary to a pickle file
    with open(results_folder_addr / 'dev.pkl', 'wb') as f:
        pickle.dump(dev_ds, f)
    # test
    test_ds = {
        'dim_process' : dim_process,
        'test' : dataset[train_num+dev_num:-1],
    }


    # Save the dictionary to a pickle file
    with open(results_folder_addr / 'test.pkl', 'wb') as f:
        pickle.dump(test_ds, f)

    

def create_training_dataset_event_window(this_db_events_v2, dim_process, window_size_events, max_num_packet_types, config):
    
    # select the source configuration
    dataset_size_max = config['dataset_size_max']

    reached_the_end = False
    dataset = []
    idx = 0
    # iterate backwards over the this_db_events_v2 
    for idx in range(len(this_db_events_v2)-1, -1, -1):
        # never start (actually end) with packet arrival
        if this_db_events_v2[idx]['packet_or_segment']:
            continue
        events_window_v3 = []
        packet_ids_set = set()
        idy = idx
        while True:
            if idy == 0:
                reached_the_end = True
                break
            if len(events_window_v3) >= window_size_events:
                break
            events_window_v3.append(this_db_events_v2[idy])
            packet_ids_set.add(this_db_events_v2[idy]['packet_id'])
            idy = idy-1
        if reached_the_end:
            break

        # sort the events_window_v3 based on timestamp
        events_window_v3 = sorted(events_window_v3, key=lambda x: x['timestamp'], reverse=False)

        # make a mapping for packet_ids, maximum packet_id becomes 0, and the rest are shifted by 1
        packet_ids_list = list(packet_ids_set)
        sorted_packet_ids_list = sorted(packet_ids_list, reverse=True)
        event_id_packet_id_mapping = {}
        for pos, packet_id in enumerate(sorted_packet_ids_list):
            event_id_packet_id_mapping[packet_id] = pos % max_num_packet_types

        events_window_v4 = []
        for pos, event in enumerate(events_window_v3):
            if event['packet_or_segment']:
                type_event = event_id_packet_id_mapping[event['packet_id']]+max_num_packet_types
            else:
                type_event = event_id_packet_id_mapping[event['packet_id']]

            # add the event to the dataset
            # remove 'packet_or_segment' 'packet_id' and 'timestamp' fields
            events_window_v4.append(
                {
                    'idx_event' : pos,
                    'type_event': type_event,
                    'len' : event['len'],
                    'mcs_index' : event['mcs_index'],
                    'mac_retx' : event['mac_retx'],
                    'rlc_failed' : event['rlc_failed'],
                    'num_rbs' : event['num_rbs'],
                    'num_symbols' : event['num_symbols'],
                    'time_since_start' : event['time_since_start'],
                    'time_since_last_event' : event['time_since_last_event']
                }
            )
        

        dataset.append(events_window_v4)
        if len(dataset) > dataset_size_max:
            break

    return dataset