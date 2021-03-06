import pandas as pd
from pulp import *
import numpy as np
import datetime
from datetime import datetime as dtm
import time
import timeit
#from pprint import pprint as pp
#import warnings
#import argparse
import os
from collections import namedtuple
#from pythoncom import com_error
#import xml.etree.ElementTree as ET
#import xml
from functools import reduce
from pandas import ExcelWriter

#------------------------------------------------------------------------------------------
directory = os.getcwd()
os.chdir(directory)
def run_model_StoExp(supplycap,supplycost,arccap,arccost,arcmin,dmd,tariff_surc,
                  sto_par_df_db, inj_cost_db, ext_cost_db,exp_price_db,exp_cap_db,Forward12m = False):

    # Add one more column for every dataframe

    dataframelist = [supplycap,supplycost,arccap,arccost,arcmin,dmd,
                     sto_par_df_db, inj_cost_db, ext_cost_db, exp_price_db, exp_cap_db]
    for dataframe in dataframelist:
        dataframe['date'] = pd.to_datetime(dataframe['date'])
        dataframe['date'] = pd.to_datetime(dataframe['date'],errors='coerce',format = '%Y-%m-%d').dt.date
        dataframe['str_date'] = dataframe['date'].apply(lambda x: x.strftime('%m-%Y'))


    # Input Supply - supply capacity data
    ## pickup data
    supply_cap,stringy_dates, actual_dates = _get_restricted_data(supplycap)
    # get ref cols
    supply_cap_cols = supply_cap.columns.values.tolist()
    ref_cols = get_ref_cols(supply_cap_cols,['str_date','date','capacity'])

    # remember the unique values for date & ref cols in the supply capacity
    # so that we can validate the cost dataset is aligned
    sup_valid_rules = {'date': actual_dates}
    for sup_col in ref_cols:
        sup_valid_rules[sup_col] = supply_cap[sup_col].unique()

    # now supply pricing data
    supply_cost = _get_restricted_data(supplycost,valid_rules=sup_valid_rules)[0]

    # validate data shape (i.e. number of rows/ cols)
    if supply_cost.shape != supply_cap.shape:
        raise ValueError('misaligned data structure detected in '
                         'tbl_supply_capacity vs tbl_supply_cost'
                         ' (they must be indentical)')

    # merge supply capacity and cost
    supply = pd.merge(supply_cap, supply_cost, 'inner',
                      on=ref_cols.append('str_date'))

    ## arc capacity
    arc_valid_rules = {'date': actual_dates}
    arc_cap = _get_restricted_data(arccap,valid_rules=arc_valid_rules)[0]
    arc_cap_cols = arc_cap.columns.values.tolist()
    arc_ref_cols = get_ref_cols(arc_cap_cols,['str_date','date','capacity'])

    # arc costs
    for col in ['Unique_From_Hub_ID','Unique_To_Hub_ID','from_hub', 'to_hub','arc_name']:
        arc_valid_rules[col] = arc_cap[col].unique()
    arc_cost = _get_restricted_data(arccost,valid_rules=arc_valid_rules)[0]
    arc_cost_cols = arc_cost.columns.values.tolist()
    arc_cost_ref_cols = get_ref_cols(arc_cost_cols,['str_date','date','cost_pesoGJ'])
    arc_valid_rules1 = {'date': actual_dates}

    arc_min_flow = _get_restricted_data(arcmin,valid_rules=arc_valid_rules1)[0]
    arc_min_flow.min_flow = arc_min_flow.min_flow.astype(float)

    # only validate the number of rows, because arc cap can have more columns
    if arc_cost.shape[0] != arc_cap.shape[0]:
        raise ValueError('misaligned data structure detected in '
                         'tbl_arc_cost vs tbl_arc_capacity '
                         '(they must be aligned)')

    # merge arc capacity & cost
    arcs = pd.merge(arc_cap, arc_cost, 'inner',
                    on=arc_cost_ref_cols.append('str_date'))

    # calculate arc tariffs
    tariff_surcharges = tariff_surc
    arcs['join_key'] = 1
    # need a temporary dummy column called join_key to link the tables together
    tariff_surcharges['join_key'] = 1
    # this next merge will multiply the number of records in arcs by
    # however many records there are in tariff_surcharges
    # (it's essentially an outer join)
    arcs = pd.merge(arcs, tariff_surcharges, on='join_key', how='inner')
    arcs.drop('join_key', axis=1, inplace=True)
    arcs['multiplier']=pd.to_numeric(arcs['multiplier'])
    arcs['capacity_portion']=pd.to_numeric(arcs['capacity_portion'])
    arcs['cost_pesoGJ'] = arcs['cost_pesoGJ'] * arcs['multiplier']
    arcs['capacity'] = arcs['capacity'] * arcs['capacity_portion']

    # bring in peso exchange rate
    # TODO: remove the date_map from here
    # (it's already in _get_unpivoted_data')
    date_map = dict(zip(stringy_dates, actual_dates))

    # fx_rate = get_df_from_table('Pipeline Tariff', 'tbl_FXrate')
    # fx_rate = fx_rate.transpose().reset_index().iloc[1:]
    # fx_rate.rename(columns={'index': 'str_date', 0: 'pesoUSD'},inplace=True)
    # fx_rate['date'] = fx_rate['str_date'].apply(lambda x: date_map[x])
    # arcs = pd.merge(arcs, fx_rate, how='inner', on=['str_date', 'date'])
    
    # convert to USD/mmBtu
    # GJ per mmBtu constant, source: ISO 80000-5
    if (arcs['topology'] =='Southern Cone').all() == True:
        arcs['cost_USDmmBtu'] = arcs['cost_pesoGJ']
    else:
        gj_mmBtu=1.055056 
        pesoUSD = 18.5
        arcs['cost_USDmmBtu'] = arcs['cost_pesoGJ'] / pesoUSD * gj_mmBtu
        arcs['cost_USDmmBtu'] = arcs['cost_USDmmBtu'].astype(np.float64).round(4)

    # demand data
    dmd_valid_rules = {'date': actual_dates}
    demand = _get_restricted_data(dmd,valid_rules=dmd_valid_rules)[0]

    #-WF_20181008--Step1 : Add parameters for storage
    # Start
    #-- Storage Data Part 1 -- Parameters that are only related to the locations of storage facilities 
    sto_valid_rules_1 = {'date': actual_dates}
    sto_par_df = _get_restricted_data(sto_par_df_db,valid_rules=sto_valid_rules_1)[0] 
    
    #-get daily data for maximum storage capacity-(mmcmd)---------
    sto_par_df['DATE'] = pd.to_datetime(sto_par_df['date'],errors='coerce',format = '%Y-%m-%d')
    sto_par_df['day'] = sto_par_df['DATE'].dt.daysinmonth

    #-- Storage Data Part 2 -- Parameters that are related to the locations of storage facilities and hubs 
    sto_valid_rules_2 = {'date': actual_dates}
    inj_cost = _get_restricted_data(inj_cost_db,valid_rules=sto_valid_rules_2)[0]
    #inj_cost_cols = inj_cost.columns.values.tolist()
    #injCost_ref_cols = get_ref_cols(inj_cost_cols,['str_date','date','inj_cost'])

    #for col_sto in injCost_ref_cols:
    for col_sto in ['hub','sto_facility']:
        sto_valid_rules_2[col_sto] = inj_cost[col_sto].unique()
    # injection and extraction need to be separated 

    ext_cost = _get_restricted_data(ext_cost_db,valid_rules=sto_valid_rules_2)[0]
    
    # export
    exp_valid_rules = {'date': actual_dates}
    exp_price = _get_restricted_data(exp_price_db,valid_rules = exp_valid_rules)[0] 
    exp_price_cols = exp_price.columns.values.tolist()
    exp_ref_cols = get_ref_cols(exp_price_cols,['str_date','date','FOB_price'])

    for col_exp in ['Unique_Hub_ID', 'Unique_ExpNode_ID','hub','node']:
        exp_valid_rules[col_exp] = exp_price[col_exp].unique()
    exp_cap = _get_restricted_data(exp_cap_db,valid_rules = exp_valid_rules)[0]

    # validate data shape (i.e. number of rows/ cols)
    if exp_price.shape != exp_cap.shape:
        raise ValueError('misaligned data structure detected in '
                         'tbl_export_price vs tbl_export_capacity'
                         ' (they must be indentical)')

    # merge supply capacity and cost
    export = pd.merge(exp_price, exp_cap, 'inner',
                      on = exp_ref_cols.append('str_date'))


    #--End (WF_20181008)
    print(supply.head())
    dflst = [supply,arcs,arc_min_flow,demand,
              sto_par_df,inj_cost,ext_cost, export]
    for df in dflst:
        df = df.sort_values(['date']).copy()

    #--------------------------------------------------------------------------------

    # by this point we have all our input data (supply, arcs, minflows, demand, sto_par_df, inj_cost, ext_cost)
    results = namedtuple('NeMo_results', ['production', 'prices','flows', 'solver_info','gas_invt','gas_inj','gas_ext','gas_export'])
    results.production = pd.DataFrame()
    results.prices = pd.DataFrame()
    results.flows = pd.DataFrame()
    results.solver_info = pd.DataFrame()
    results.gas_invt = pd.DataFrame()
    results.gas_inj = pd.DataFrame()
    results.gas_ext = pd.DataFrame()
    results.gas_export = pd.DataFrame()
    actual_dates = sorted(actual_dates)
    initial_storage = {}

    
    # PROCESS
    tStart = dtm.now()
    if Forward12m == False:
        # by calendar year
        #-------------------------------------------------------------
        gas_invt_df = pd.DataFrame() 

        for i in range(0,len(actual_dates),12):
            d_range = actual_dates[i:i + 12]
            ID = round(i/12)
            print(i,ID,d_range)
            
            d_string = d_range[-1].strftime('%m-%Y')
            print(d_string)
            
            if i == 0:
                sto_ls = sto_par_df['sto_facility'].unique().tolist()
                init_sto_first = pd.DataFrame(sto_ls, columns=['sto_facility'])
                init_sto_first['init_sto'] = float(0)
            else:
                init_sto_first = initial_storage[ID-1]
            
            solution = solve_network(ID,d_range,supply,arcs,arc_min_flow,demand,sto_par_df,inj_cost,ext_cost,init_sto_first,export)
            
            gas_invt_df = solution[4][solution[4]['str_date']==d_string]
            
            initial_storage[ID] = gas_invt_df.copy()
            initial_storage[ID] = initial_storage[ID].rename(columns = {'storage_facility':'sto_facility','gas_inventory':'init_sto'})
            
            results.production = results.production.append(solution[0])
            results.prices = results.prices.append(solution[1])
            results.flows = results.flows.append(solution[2])
            results.solver_info = results.solver_info.append(solution[3])  
            results.gas_invt = results.gas_invt.append(solution[4])
            results.gas_inj = results.gas_inj.append(solution[5])
            results.gas_ext = results.gas_ext.append(solution[6])
            results.gas_export = results.gas_export.append(solution[7])

    else:
    
        # current month plus the next 11 months
        #-------------------------------------------------------------------------------------
        
        act_dates = actual_dates[:len(actual_dates)-11]
        
        for date in act_dates:
            dateID = actual_dates.index(date)
            print(date,dateID)
            dateRange = actual_dates[dateID:dateID+12]
            print(dateRange)
            
            # if the date is not the last element of the 'stry_dates' list
            # keep the solution for the first date in dateRange
            if date != act_dates[-1]:
                #print('ok')
                d_string = date.strftime('%m-%Y')
                print(d_string)
                
                if dateID == 0:
                    sto_ls = sto_par_df['sto_facility'].unique().tolist()
                    init_sto_first = pd.DataFrame(sto_ls, columns=['sto_facility'])
                    init_sto_first['init_sto'] = float(0)
                else:
                    init_sto_first = initial_storage[dateID-1]
                    
                
                solution = solve_network(dateID,dateRange,supply,arcs,arc_min_flow,demand,sto_par_df,inj_cost,ext_cost,init_sto_first,export)
                production = solution[0][solution[0]['str_date']==d_string]
                prices = solution[1][solution[1]['str_date']==d_string]
                flows = solution[2][solution[2]['str_date']==d_string]
                
                gas_invt_df = solution[4][solution[4]['str_date']==d_string]
                gas_inj_df = solution[5][solution[5]['str_date']==d_string]
                gas_ext_df = solution[6][solution[6]['str_date']==d_string]

                gas_exp_df = solution[7][solution[7]['str_date']==d_string]
                
                initial_storage[dateID] = gas_invt_df.copy()
                initial_storage[dateID] = initial_storage[dateID].rename(columns = {'storage_facility':'sto_facility','gas_inventory':'init_sto'})
                
                
                results.production = results.production.append(production)
                results.prices = results.prices.append(prices)
                results.flows = results.flows.append(flows)
                results.solver_info = results.solver_info.append(solution[3])
                results.gas_invt = results.gas_invt.append(gas_invt_df)
                results.gas_inj = results.gas_inj.append(gas_inj_df)
                results.gas_ext = results.gas_ext.append(gas_ext_df)
                results.gas_export = results.gas_export.append(gas_exp_df)
                
                
            # if the date is the last element of the 'stry_dates' list
            # keep the solution for the whole year
            else:
                print('ok1',dateID-1)
                init_sto_first = initial_storage[dateID-1]
                
                last_solve = solve_network(dateID,dateRange,supply,arcs,arc_min_flow,demand,sto_par_df,inj_cost,ext_cost,init_sto_first,export)
                
                results.production = results.production.append(last_solve[0])
                results.prices = results.prices.append(last_solve[1])
                results.flows = results.flows.append(last_solve[2])
                results.solver_info = results.solver_info.append(last_solve[3])
                results.gas_invt = results.gas_invt.append(last_solve[4])
                results.gas_inj = results.gas_inj.append(last_solve[5])
                results.gas_ext = results.gas_ext.append(last_solve[6])
                results.gas_export = results.gas_export.append(last_solve[7])
                
    tEnd = dtm.now()   
    print ("Run nemo model : " + str(tEnd-tStart))
 
#------------------------------------------------
#-WF_20181008-- Step3: Add storage outputs---
#-------------------------------------------------
#-----------------
#---arc---------------------
    arcs_1 = arcs.copy()
    arcs_1.set_index(['from_hub', 'to_hub', 'tranche', 'str_date'], inplace=True)

    results.flows_1 = results.flows.copy()

    results.flows_1.set_index(['from_hub', 'to_hub', 'tranche', 'str_date'], inplace=True)

    full_solved_arcs = pd.merge(arcs_1, results.flows_1, left_index=True,
                                right_index=True)
    full_solved_arcs.set_index(['Unique_From_Hub_ID','Unique_To_Hub_ID','date','arc_name','case_id','topology'], append=True, inplace=True)
    full_solved_arcs = full_solved_arcs[['capacity', 'flow']]

    # aggregate all the tranches together
    full_solved_arcs = full_solved_arcs.groupby(level=['Unique_From_Hub_ID','Unique_To_Hub_ID','from_hub', 'to_hub','arc_name','str_date','case_id','topology','date']).sum()

    full_solved_arcs['utilisation'] = full_solved_arcs['flow']/full_solved_arcs['capacity']

    full_solved_arcs = full_solved_arcs.reset_index()

    #-demand--------------------
    demand_1 = demand.copy()
    demand_1.set_index(['node', 'str_date','date'], inplace=True)

    results.prices_1 = results.prices.copy()

    results.prices_1.set_index(['node', 'str_date','date'], inplace=True)

    full_solved_demand = pd.merge(demand_1, results.prices_1, left_index=True,
                                  right_index=True)
    full_solved_demand = full_solved_demand.reset_index()
    #-supply----------------------
    supply_1 = supply.copy()
    supply_1.set_index(['node', 'hub', 'str_date'], inplace=True)
    results.production_1 = results.production.copy()

    results.production_1.set_index(['node', 'hub', 'str_date'], inplace=True)

    # combine source data & solved data to ensure we have all the data
    full_solved_supply = pd.merge(supply_1, results.production_1, left_index=True,
                                  right_index=True)

    full_solved_supply = full_solved_supply.reset_index()


    #--
    #-- storage injection
    inj_cost_1 = inj_cost.copy()
    inj_cost_1.set_index(['hub', 'sto_facility','str_date'], inplace=True)

    results.gas_inj_1 = results.gas_inj.copy()


    results.gas_inj_1.set_index(['hub', 'sto_facility','str_date'], inplace=True)

    full_solved_inj = pd.merge(inj_cost_1,results.gas_inj_1,left_index=True,right_index=True)

    full_solved_inj = full_solved_inj.reset_index()

    #-- storage extraction
    ext_cost_1 = ext_cost.copy()
    ext_cost_1.set_index(['sto_facility','hub','str_date'], inplace=True)

    results.gas_ext_1 = results.gas_ext.copy()

    results.gas_ext_1.set_index(['sto_facility','hub','str_date'], inplace=True)

    full_solved_ext = pd.merge(ext_cost_1,results.gas_ext_1,left_index=True,right_index=True)
    full_solved_ext = full_solved_ext.reset_index()


    #-- gas staying in storage facilities
    sto_par_df_1 = sto_par_df.copy()
    sto_par_df_1.set_index(['sto_facility','str_date'], inplace=True)

    results.gas_invt_1 = results.gas_invt.copy()


    results.gas_invt_1.set_index(['sto_facility','str_date'], inplace=True)

    full_solved_stoInvt = pd.merge(sto_par_df_1,results.gas_invt_1,left_index=True,right_index=True)
    full_solved_stoInvt = full_solved_stoInvt.reset_index()

    # export results
    export_1 = export.copy()
    export_1.set_index(['hub', 'node', 'str_date'], inplace=True)
    
    results.gas_export_1 = results.gas_export.copy()
    results.gas_export_1.set_index(['hub', 'node', 'str_date'], inplace=True)
    
    full_solved_export = pd.merge(export_1,results.gas_export_1,left_index=True,right_index=True)
    full_solved_export = full_solved_export.reset_index()

    # optimal or not
    solver_status = results.solver_info

#---------------------


    return full_solved_supply, full_solved_demand, full_solved_arcs, solver_status, full_solved_inj, full_solved_ext, full_solved_stoInvt, full_solved_export

    #-WF_20181008-- Step2: Add storage variables and change the loop--- 
def solve_network(dateID,dates,supply,arcs,arc_min_flow,demand,sto_par_df,inj_cost,ext_cost,init_sto_first,export):
    """
        runs the linear optimisation model for each year

        parameters:
            :supply:pandas df (TODO: more info about requirements)
            :arcs:  pandas df (TODO: more info about requirements)
            :arc_min_flow: pandas df
            :demand:pandas df (TODO: more info about requirements)
            :dates: list of unique dates

        returns:
            results:    namedtuple of dataframes
                        (production, flows, solver_info, prices)
    """

    # since we package up run_opm we need to tell it where the cbc.exe lives
    # solverdir = 'C:\\Users\\fanxin\\AppData\\Local\\Continuum\\anaconda3\\Lib\\site-packages\\pulp\\solverdir\\cbc\\win\\64\\cbc.exe' 
    # 'D:\\Users\\fanxin\\.virtualenvs\\test\\Lib\\site-packages\\pulp\\solverdir\\cbc\\win\\64\\cbc.exe'
    
    #'D:\\Users\\fanxin\\.virtualenvs\\test\\Lib\\site-packages\\pulp\\solverdir\\cbc\\win\\64\\cbc.exe'
    # it'll be in the cwd since all exes are on same level
    solverdir = os.path.join(os.getcwd(), 'nemo_env\\Lib\\site-packages\\pulp\\solverdir\\cbc\\win\\64\\cbc.exe')

    solver = COIN_CMD(path=solverdir)
    dates = sorted(dates)

    # WF_20181120
    # get the first and end date
    first = dates[0]
    end = dates[-1]
    
    # get stringy dates from dates 
    stry_dates = []
    for i in range(len(dates)):
        stry_date = dates[i].strftime('%m-%Y')
        stry_dates.append(stry_date)
    
    stry_dates = sorted(stry_dates, key=lambda x: datetime.datetime.strptime(x, '%m-%Y'))


    # filter each df in the range of dates
    supply_t = supply[(supply['date'] >= first) & (supply['date'] <= end)].copy()
    arcs_t = arcs[(arcs['date'] >= first) & (arcs['date'] <= end)].copy()
    arc_min_flow_t = arc_min_flow[(arc_min_flow['date'] >= first) & (arc_min_flow['date'] <= end)].copy()
    demand_t = demand[(demand['date'] >= first) & (demand['date'] <= end)].copy()
    # demand

    #-WF_20181009--
    #-- storage
    sto_par_df_t = sto_par_df[(sto_par_df['date'] >= first) & (sto_par_df['date'] <= end)].copy()
    
    inj_cost_t = inj_cost[(inj_cost['date'] >= first) & (inj_cost['date'] <= end)].copy()
    ext_cost_t = ext_cost[(ext_cost['date'] >= first) & (ext_cost['date'] <= end)].copy()
    #-- End (WF_20181009)

    #-export
    export_t = export[(export['date'] >= first) & (export['date'] <= end)].copy()  

    #----------------------------------------------------------------

    # get all potential suppliers for this year

    # --------------MODEL CREATION/CONFIG-----------------
    # Variables
    
    # supply
    arcs_sh_dt = [tuple(x) for x in supply_t[['node', 'hub','str_date' ]].values]
        
    lpvar_sales = LpVariable.dicts('Flow_sales', ((s, h, dt ) for s, h, dt in arcs_sh_dt), 0)

    # demand
    demand_hd_dt = [tuple(x) for x in demand_t[['hub','node','str_date']].values]
   
    lpvar_flow_hd = LpVariable.dicts('Flow_hd', ((h,d,dt) for h,d,dt in demand_hd_dt), 0)

    # arc flow
    arcs_hh_t_dt = [tuple(x) for x in arcs_t[['from_hub', 'to_hub','tranche','str_date']].values]

    lpvar_flow_hh = LpVariable.dicts('lpvar_flow', ((from_h,to_h,tranch,dt) for from_h,to_h,tranch,dt in arcs_hh_t_dt) , 0)

    # WF-20181120_storage
    injs_h_sto_dt = [tuple(x) for x in inj_cost_t[['hub', 'sto_facility','str_date']].values]
    lpvar_inj_sto = LpVariable.dicts('Inj_h_sto', ((h,sto,dt) for h,sto,dt in injs_h_sto_dt) , 0)
    
    exts_sto_h_dt = [tuple(x) for x in ext_cost_t[['sto_facility','hub','str_date']].values]
    lpvar_ext_sto = LpVariable.dicts('Ext_sto_h', ((sto,h,dt) for sto,h,dt in exts_sto_h_dt) , 0)
    
    sto_facility = [tuple(x) for x in sto_par_df_t[['sto_facility','str_date']].values]
    lpvar_gas_sto = LpVariable.dicts('Gas_sto', ((sto,dt) for sto,dt in sto_facility), 0)
    
    # export
    exp_hl_dt = [tuple(x) for x in export_t[['hub', 'node','str_date']].values]
        
    lpvar_export = LpVariable.dicts('Flow_export', ((h ,l, dt ) for h, l, dt in exp_hl_dt), 0)

    # Declare model
    prob = LpProblem('MiniLP', LpMinimize)

    # Equations
    # cost of supply
    
    costs_s = supply_t.set_index(['node','hub','str_date'])['cost'].to_dict()

    eqn_cost_s = [lpvar_sales[(s,h,dt)] * cost for (s, h, dt),cost in costs_s.items()]
    
    # cost of transit
    costs_hh = arcs_t.set_index(['from_hub', 'to_hub', 'tranche','str_date'])[
        'cost_USDmmBtu'].to_dict()

    eqn_cost_hh = [lpvar_flow_hh[(hin,hout,tranche,dt)] * cost
                   for (hin, hout, tranche,dt), cost in costs_hh.items()]
    
    #-WF_20181010--
    #-- storage
    #--- 1. cost of gas injection from hubs to sto_facilities
    costs_inj = inj_cost_t.set_index(['hub', 'sto_facility','str_date'])['inj_cost'].to_dict()

    eqn_cost_inj = [lpvar_inj_sto[(h,sto,dt)] * cost for (h,sto,dt), cost in costs_inj.items()]

    #--- 2. cost of gas extraction from sto_facilites to hubs
    costs_ext = ext_cost_t.set_index(['sto_facility','hub','str_date'])['ext_cost'].to_dict()

    eqn_cost_ext = [lpvar_ext_sto[(sto,h,dt)] * cost for (sto, h,dt), cost in costs_ext.items()]

    #--- 3. cost of gas storing in facilities
    #costs_storing = sto_par_df_t.set_index(['sto_facility','str_date'])['storing_cost'].to_dict()
    #eqn_cost_storing = [lpvar_gas_sto[(sto,dt)] * cost for (sto,dt), cost in costs_storing.items()]  
    # storing cost changed  
    costs_storing_1 = sto_par_df_t[['sto_facility','str_date','storing_cost','day']].copy()
    costs_storing_2 = costs_storing_1.set_index(['sto_facility','str_date']).T.to_dict('list')

    eqn_cost_storing_1 = [lpvar_gas_sto[(sto,dt)] * cost *(1/day) for (sto,dt), (cost, day) in costs_storing_2.items()]
    
    # revenue of export
    price_exp = export_t.set_index(['hub', 'node','str_date'])['FOB_price'].to_dict()

    eqn_profit_exp = [lpvar_export[(h,l,dt)] * price for (h,l,dt), price in price_exp.items()]

    # objective function
    prob += lpSum(eqn_cost_s) + lpSum(eqn_cost_hh) + lpSum(eqn_cost_inj) + lpSum(eqn_cost_ext) + lpSum(eqn_cost_storing_1) - lpSum(eqn_profit_exp), 'Sum_Costs - Export_Revenue'

    # supply maximum constraints for each supply node
    cap_s = supply_t.set_index(['node','hub','str_date'])['capacity'].to_dict()
     
    for (s, h, dt) in arcs_sh_dt:
        prob += lpvar_sales[(s,h,dt)] <= cap_s[(s,h,dt)], 'CapC_%s_%s' % (s,dt)

    # demand minimum constraints
    demand_hd = demand_t.set_index(['hub', 'node','str_date'])['demand'].to_dict()

    for (h, d,dt) in demand_hd:
        prob += lpvar_flow_hd[(h,d,dt)] >= demand_hd[(h,d,dt)], "DemC_%s_%s" % (d,dt)

    # export maximum constraints for each export node
    cap_exp = export_t.set_index(['hub','node','str_date'])['capacity'].to_dict()
     
    for (h, l, dt) in cap_exp:
        prob += lpvar_export[(h, l, dt)] <= cap_exp[(h, l, dt)], 'ExpC_%s_%s' % (l,dt)

    # arc capacity constraints
    cap_hh = arcs_t.set_index(['from_hub', 'to_hub', 'tranche','str_date'])[
            'capacity'].to_dict()

    for (hin, hout, tranche, dt) in cap_hh:
        id_hh = (hin, hout, tranche, dt)  # unique id (tuple)
        cap_constraint = lpvar_flow_hh[hin, hout, tranche, dt] <= cap_hh[id_hh]
        # capacity
        prob += cap_constraint, 'ArcC_%s' % '_'.join(id_hh)
        
    # min flow (which isn't specific to tranche)
    tranches = arcs['tranche'].unique()
    min_hh = arc_min_flow_t.set_index(['from_hub', 'to_hub','str_date'])[
            'min_flow'].to_dict()  # minflow doesn't have tranche
    
    for (hin, hout,dt) in min_hh:
        id_hh_min = (hin, hout,dt)
        if min_hh[id_hh_min] > 0:
            min_constraint = lpSum(lpvar_flow_hh[hin,hout,t,dt]
                                   for t in tranches) >= min_hh[id_hh_min]
            prob += min_constraint, 'ArcMin_%s' % '_'.join(id_hh_min)
        # else:
            # nothing - ignore zero min flows for efficiency     
            
    # add the hub mass balance constraint
    def uniquify(series):
        """shortcut to assign unique series values to a list"""
        return series.unique().tolist()
                      
    #-- storage
    #--- 1. capacity constraints
    #--- 1.1 maximum injection
    
    max_inj_sto = sto_par_df_t.set_index(['sto_facility','str_date'])['max_inj'].to_dict()
    
    for (sto,dt) in max_inj_sto:
        inj_cost_m = inj_cost_t[inj_cost_t['str_date'] == dt]
        inj_hub = uniquify(inj_cost_m[inj_cost_m['sto_facility'] == sto]['hub'])
        
        max_inj_constraint = lpSum(lpvar_inj_sto[(h,sto,dt)] for h in inj_hub)<= max_inj_sto[(sto,dt)]
        #print(max_inj_constraint)
        prob += max_inj_constraint, 'injctionMax_%s_%s' % (sto,dt)


    #--- 1.2 maximum extraction
    max_ext_sto = sto_par_df_t.set_index(['sto_facility','str_date'])['max_ext'].to_dict()

    for (sto,dt) in max_ext_sto:
        ext_cost_m = ext_cost_t[ext_cost_t['str_date'] == dt]
        ext_hub = uniquify(ext_cost_m[ext_cost_m['sto_facility'] == sto]['hub'])
        
        max_ext_constraint = lpSum(lpvar_ext_sto[(sto,h,dt)] for h in ext_hub) <= max_ext_sto[(sto,dt)]
        prob += max_ext_constraint, 'extractionMax_%s_%s' % (sto,dt)

    #--- 1.3 minimum gas storing in facilities
    min_gas_storing = sto_par_df_t.set_index(['sto_facility','str_date'])['min_sto_cap'].to_dict()

    for (sto,dt) in sto_facility:
        prob += lpvar_gas_sto[(sto,dt)] >= min_gas_storing[(sto,dt)], "Min_storing_cap_%s_%s" % (sto,dt)

    #--- 1.4 maximum gas storing in facilities
    max_gas_storing = sto_par_df_t.set_index(['sto_facility','str_date'])['max_sto_cap'].to_dict()

    for (sto,dt) in sto_facility:
        prob += lpvar_gas_sto[(sto,dt)] <= max_gas_storing[(sto,dt)], "Max_storing_cap_%s_%s" % (sto,dt)    


    #-WF_20181010--
    #-- Pipeline balance constraint
    #-- add storage
    #--- 2. change or add gas flow constraints considering gas storage 
    #--- 2.1 add interactions between hubs and storage facilities 
    #---     (gas injection and extraction) into hub flow constraint 
#---loop through date
    for dt in stry_dates:
        #print(dt)
        arcs_m = arcs_t[arcs_t['str_date'] == dt]
        #print(arcs_m.head())
        supply_m = supply_t[supply_t['str_date'] == dt]
        demand_m = demand_t[demand_t['str_date'] == dt]
        
        export_m = export_t[export_t['str_date'] == dt]

        inj_cost_m = inj_cost_t[inj_cost_t['str_date'] == dt]
        ext_cost_m = ext_cost_t[ext_cost_t['str_date'] == dt]
        
        hubs = sorted(set(list(arcs_m['from_hub']) + list(arcs_m['to_hub'])))
        
        for h in hubs:
            #print(h)
            in_hubs = uniquify(arcs_m[arcs_m['to_hub'] == h]['from_hub'])
            out_hubs = uniquify(arcs_m[arcs_m['from_hub'] == h]['to_hub'])
            hub_suppliers = uniquify(supply_m[supply_m['hub'] == h]['node'])
            hub_demanders = uniquify(demand_m[demand_m['hub'] == h]['node'])
            
            hub_exports = uniquify(export_m[export_m['hub'] == h]['node'])

            #------------------------------------------
            # injection from hub to storage facility
            inj_sto = uniquify(inj_cost_m[inj_cost_m['hub'] == h]['sto_facility'])
            # extraction from storage facility to hub
            ext_sto = uniquify(ext_cost_m[ext_cost_m['hub'] == h]['sto_facility'])
                
            hflows_sh = [lpvar_sales[s,h,dt] for s in hub_suppliers ]
            hflows_in_hh = [lpvar_flow_hh[in_h,h,t,dt] for in_h in in_hubs for t in tranches]
            # ohh the hokey cokey
            hflows_out_hh = [lpvar_flow_hh[h,out_h,t,dt] for out_h in out_hubs for t in tranches]
            # ohhhhhhhhh the hokey cokey
            # lpvar_flow_hh is the model variable for hub to hub flows
            # it's referenced in the format [from_node][to_node]
            # the previous 2 lines ; 
            hflows_hd = [lpvar_flow_hd[h,d,dt] for d in hub_demanders]

            # hub exports
            hflows_h_exp = [lpvar_export[h,l,dt] for l in hub_exports]
            
            #------------------------------------------
            # gas injection from hub to storage facility 
            hflows_inj_sto = [lpvar_inj_sto[h,sto,dt] for sto in inj_sto]
            # gas extraction from storage facility to hub
            hflows_ext_sto = [lpvar_ext_sto[sto,h,dt] for sto in ext_sto]
    
            prob += lpSum(hflows_sh) + lpSum(hflows_in_hh)+ lpSum(hflows_ext_sto)\
                == lpSum(hflows_hd) + lpSum(hflows_out_hh) + lpSum(hflows_inj_sto) + lpSum(hflows_h_exp), 'HMBC_%s_%s' % (h,dt)
    
    #-WF_20181010--
    #-- storage facility balance constraint    
    init_sto = init_sto_first.set_index('sto_facility')['init_sto'].to_dict()
    sto_tp = [tuple(x) for x in sto_par_df_t[['sto_facility', 'str_date']].values]
    for sto,dt in sto_tp:
        #print(sto,dt)
        inj_cost_m = inj_cost_t[inj_cost_t['str_date'] == dt]
        ext_cost_m = ext_cost_t[ext_cost_t['str_date'] == dt]
        sto_par_df_m = sto_par_df_t[sto_par_df_t['str_date'] == dt]
        day = sto_par_df_m['day'].unique().astype(float)
        
        if dt == stry_dates[0]:
            print('ok')
            
            # injection from hub to storage facility
            inj_hub = uniquify(inj_cost_m[inj_cost_m['sto_facility'] == sto]['hub'])
            inj_from_h = [lpvar_inj_sto[h,sto,dt] for h in inj_hub]
            
            # extraction from storage facility to hub
            ext_hub = uniquify(ext_cost_m[ext_cost_m['sto_facility'] == sto]['hub'])
            ext_to_h = [lpvar_ext_sto[sto,h,dt] for h in ext_hub]
            prob += init_sto[sto] + lpSum(inj_from_h)*day == lpvar_gas_sto[sto,dt] + lpSum(ext_to_h)*day , 'Sto_BA_%s_%s' % (sto,dt)
                
        else:
            print('ok_1')
            # injection from hub to storage facility
            inj_hub = uniquify(inj_cost_m[inj_cost_m['sto_facility'] == sto]['hub'])
            inj_from_h = [lpvar_inj_sto[h,sto,dt] for h in inj_hub]
            
            # extraction from storage facility to hub
            ext_hub = uniquify(ext_cost_m[ext_cost_m['sto_facility'] == sto]['hub'])
            ext_to_h = [lpvar_ext_sto[sto,h,dt] for h in ext_hub]
            
            date_ID = stry_dates.index(dt)
            dt_1 = stry_dates[date_ID-1]
            print(dt_1)
            prob += lpvar_gas_sto[sto,dt_1] + lpSum(inj_from_h)*day == lpvar_gas_sto[sto,dt] + lpSum(ext_to_h)*day , 'Sto_BA_%s_%s' % (sto,dt)        
      
    # solve the model
    prob.writeLP('{}\\MiniLP_nemo.lp'.format(directory))
    prob.solve(solver)  # https://xkcd.com/287/
    #print('solved', date) # debug
    # -------------- OUTPUT DATA -------------------
    # SUPPLY
    
    production_values = []
    for s,h,dt in lpvar_sales:
        var_prod = {
                'node': s,
                'hub': h,
                'str_date': dt,
                'production': lpvar_sales[(s,h,dt)].varValue
                        
                }
        production_values.append(var_prod)
    solved_supply = pd.DataFrame.from_records(production_values)  

    #--
    # DEMAND PRICES
    constraints = prob.constraints.items()
    dmd_prices = {k[5:]: v.pi for k, v in constraints
                  if k[:4] == 'DemC'}
    solved_prices = pd.DataFrame(dmd_prices, index=['price']
                                 ).transpose()

    # export
    exp_values = []
    for h, l, dt in lpvar_export:
        var_exp = {
                'hub': h,
                'node': l,
                'str_date': dt,
                'gas_export': lpvar_export[(h, l, dt)].varValue
                        
                }
        exp_values.append(var_exp)
    solved_export = pd.DataFrame.from_records(exp_values) 
    
    # HH_FLOWS
    flow_values = []
    for from_hub,to_hub,t,dt in lpvar_flow_hh:
        var_flow = {
                'from_hub':from_hub,
                'to_hub': to_hub,
                'tranche': t,
                'str_date': dt,
                'flow': lpvar_flow_hh[(from_hub,to_hub,t,dt)].varValue
                        
                }
        flow_values.append(var_flow)
    solved_flows = pd.DataFrame.from_records(flow_values)

    solved_flows = solved_flows.dropna()
    
    # gas stored in storage facilities
    gas_stored = []
    for sto, dt in lpvar_gas_sto:
        var_gas = {
                'sto_facility': sto,
                'str_date': dt,
                'gas_inventory': lpvar_gas_sto[(sto,dt)].varValue
                }
        gas_stored.append(var_gas)
    solved_gas_invt = pd.DataFrame.from_records(gas_stored)
    
    # gas injection
    gas_injected = []
    for h, sto, dt in lpvar_inj_sto:
        var_injected = {
                     'hub': h,
                     'sto_facility': sto,
                     'str_date': dt,
                     'gas_injection': lpvar_inj_sto[(h,sto,dt)].varValue
                }
        gas_injected.append(var_injected)
    solved_gas_injected = pd.DataFrame.from_records(gas_injected)
    
    # gas extraction
    gas_extracted = []
    for sto, h, dt in lpvar_ext_sto:
        var_extracted = {
                     'sto_facility': sto,
                     'hub': h,
                     'str_date': dt,
                     'gas_extraction': lpvar_ext_sto[(sto,h,dt)].varValue
                }
        gas_extracted.append(var_extracted)
    solved_gas_extracted = pd.DataFrame.from_records(gas_extracted)

    status = pulp.LpStatus[prob.status]
    obj_value = prob.objective.value()
    model_info = {'status': status, 'total_cost': obj_value, 'year_index': dateID}
    solver_info = pd.DataFrame(data=model_info,
                               columns=model_info.keys(), index=[0])


    solved_prices.index.rename('node_date', inplace=True)
    solved_prices_1 = solved_prices.reset_index().copy()
    solved_prices_1['year'] = solved_prices_1['node_date'].str.split('_').str[-1]
    solved_prices_1['month'] = solved_prices_1['node_date'].str.split('_').str[-2]
    solved_prices_1['node'] = solved_prices_1['node_date'].str.split('_').str[:-2].str.join('_')
    solved_prices_1['str_date'] = solved_prices_1[['month', 'year']].apply(lambda x: '-'.join(x), axis=1)
    solved_prices_1['day'] = 1
    solved_prices_1['date'] = pd.to_datetime(solved_prices_1[['year','month','day']]) 
    solved_prices_1['date'] = pd.to_datetime(solved_prices_1['date'],errors='coerce',format = '%Y-%m-%d').dt.date
    solved_prices_new = solved_prices_1[['node','str_date','date','price']].copy()

# loop to next year
    #return results
    return solved_supply, solved_prices_new, solved_flows, solver_info, solved_gas_invt, solved_gas_injected, solved_gas_extracted, solved_export

def _get_restricted_data(df, valid_rules=None):   

    stringy_dates = df['str_date'].unique().tolist()
    actual_dates = pd.to_datetime(df['date']).dt.date.unique().tolist()
    
    # validate - our valid_rules dict has a list of cols
    # & permitted unique values
    if valid_rules is not None:
        for col, valid_values in valid_rules.items():
            unique_values = df[col].unique()
            # ^ is a binary xor operator
            wonky_values = [str(x) for x in set(unique_values) ^
                            set(valid_values)]
            if len(wonky_values) > 0:
                raise ValueError('probably values appear in this table but not another (or vice-versa)')
    return df, stringy_dates, actual_dates  

# list a set of values for 
def get_ref_cols(col_name_list, sublist):
    sublist_as_set = list(sublist)
    return [ x for x in col_name_list if x not in sublist_as_set ]







