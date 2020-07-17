'''
Created on July 6, 2020

A module to load data from various station datasets as time series and convert to NetCDF

@author: Andre R. Erler, GPL v3
'''



# external imports
import datetime as dt
import pandas as pd
import os
import os.path as osp
from warnings import warn
import numpy as np
import netCDF4 as nc # netCDF4-python module
import xarray as xr
from collections import namedtuple
import inspect
# internal imports
from datasets.common import getRootFolder, grid_folder
from geodata.netcdf import DatasetNetCDF
from processing.newvars import e_sat
# for georeferencing
from geospatial.netcdf_tools import autoChunk, addTimeStamps, addNameLengthMonth
from geospatial.xarray_tools import addGeoReference, loadXArray, updateVariableAttrs, computeNormals

## Meta-vardata

dataset_name = 'ClimateStations'
root_folder = getRootFolder(dataset_name=dataset_name, fallback_name='HGS') # get dataset root folder based on environment variables

# attributes of variables in final collection
varatts = dict(precip   = dict(name='precip', units='kg/m^2/s', long_name='Total Precipitation'),
               MaxPrecip_1h = dict(name='MaxPrecip_1h', units='kg/m^2/s', long_name='Maximum Hourly Precipitation'),
               pet      = dict(name='pet', units='kg/m^2/s', long_name='PET (Penman-Monteith)'),
               pet_dgu  = dict(name='pet_dgu', units='Pa/K', long_name='PET Denominator'),
               pet_rad  = dict(name='pet_rad', units='kg/m^2/s', long_name='PET Radiation Term'),
               pet_wnd  = dict(name='pet_wnd', units='kg/m^2/s', long_name='PET Wind Term'),
               pet_hog  = dict(name='pet_hog', units='kg/m^2/s', long_name='PET (Hogg 1997)'),
               pet_har  = dict(name='pet_har', units='kg/m^2/s', long_name='PET (Hargeaves)'),
               pet_th   = dict(name='pet_th', units='kg/m^2/s', long_name='PET (Thornthwaite)'),
               pmsl     = dict(name='pmsl', units='Pa', long_name='Mean Sea-level Pressure'), # sea-level pressure
               ps       = dict(name='ps', units='Pa', long_name='Surface Air Pressure'), # surface pressure
               Ts       = dict(name='Ts', units='K', long_name='Skin Temperature'), # average skin temperature
               TSmin    = dict(name='TSmin', units='K', long_name='Minimum Skin Temperature'), # minimum skin temperature
               TSmax    = dict(name='TSmax', units='K', long_name='Maximum Skin Temperature'), # maximum skin temperature
               T2       = dict(name='T2', units='K', long_name='2m Temperature'), # 2m average temperature
               Tmin     = dict(name='Tmin', units='K', long_name='Minimum 2m Temperature'), # 2m minimum temperature
               Tmax     = dict(name='Tmax', units='K', long_name='Maximum 2m Temperature'), # 2m maximum temperature
               Q2       = dict(name='Q2', units='Pa', long_name='Water Vapor Pressure'), # 2m water vapor pressure
               Q2max    = dict(name='Q2max', units='Pa', long_name='Maximum Water Vapor Pressure'), # maximum diurnal water vapor pressure
               Q2min    = dict(name='Q2min', units='Pa', long_name='minimum Water Vapor Pressure'), # minimum diurnal water vapor pressure
               RH       = dict(name='RH', units='\%', long_name='Relative Humidity'), # 2m relative humidity
               RHmax    = dict(name='RHmax', units='\%', long_name='Maximum Relative Humidity'), # 2m diurnal maximum relative humidity
               RHmin    = dict(name='RHmin', units='\%', long_name='Minimum Relative Humidity'), # 2m diurnal minimum relative humidity
               U2       = dict(name='U2', units='m/s', long_name='2m Wind Speed'), # 2m wind speed
               U2_dir   = dict(name='U2_dir', units='deg', long_name='2m Wind Direction'), # 2m wind direction
               U2max    = dict(name='U2max', units='m/s', long_name='2m Maximum Wind Speed'), # 2m maximum diurnal wind speed
               U10      = dict(name='U10', units='m/s', long_name='10m Wind Speed'), # 2m wind speed
               U10_dir  = dict(name='U10_dir', units='deg', long_name='10m Wind Direction'), # 10m wind direction
               U10max   = dict(name='U10max', units='m/s', long_name='10m Maximum Wind Speed'), # 10m maximum diurnal wind speed
               DNSW     = dict(name='DNSW', units='W/m^2', long_name='Downward Solar Radiation'),
               UPSW     = dict(name='UPSW', units='W/m^2', long_name='Upward Solar Radiation'),
               DNLW     = dict(name='DNLW', units='W/m^2', long_name='Downward Longwave Radiation'),
               UPLW     = dict(name='UPLW', units='W/m^2', long_name='Upward Longwave Radiation'),
               DNLW_raw = dict(name='DNLW_raw', units='W/m^2', long_name='Downward Longwave Radiation (uncorrected)'),
               UPLW_raw = dict(name='UPLW_raw', units='W/m^2', long_name='Upward Longwave Radiation (uncorrected)'),
               netrad   = dict(name='netrad', units='W/m^2', long_name='Net Downward Radiation'), # radiation absorbed by the ground
               gndrad   = dict(name='gndrad', units='W/m^2', long_name='Longwave Ground Emission'), # not quite sure what this is...
               # axes
               time    = dict(name='time', units='days', long_name='Time in Days'), # time coordinate
               )
varlist = varatts.keys()
ignore_list = []

## station meta data
class StationMeta(object):
    name = None
    title = None
    region = None
    folder = None
    filelist = None
    file_fmt = None
    testfile = None
    readargs = None
    varatts = None
    minmax = None
    sampling = 'h'
    
    def __init__(self, name=None, title=None, region=None, filelist=None, filename=None, testfile=None,
                 file_fmt=None, folder=None, readargs=None, varatts=None, minmax=None, sampling='h'):
        ''' assign some values with smart defaults '''
        self.name = name
        self.title = title if title else name
        self.region = region
        self.folder = folder if folder else osp.join(root_folder,region,'source',name) # default folder
        if filename is not None: # generate filelist
            if filelist is not None: raise ValueError(filelist)
            filelist = [filename]
        self.filelist = filelist
        if file_fmt is None: # auto-detect file format
            if all(fn.lower().endswith(('.xls','.xlsx')) for fn in filelist): file_fmt = 'xls'
            elif all(fn.lower().endswith('.csv') for fn in filelist): file_fmt = 'csv'
            else:
                raise NotImplementedError('Cannot determine source format:'.format(file_fmt))
        self.file_fmt = file_fmt
        self.testfile = testfile
        self.readargs = readargs if readargs else dict()
        self.varatts = varatts if varatts else dict()
        self.minmax = minmax if minmax else dict()
        self.sampling = sampling
      
# Ontario stations
ontario_station_list = dict()
# UTMMS station
stn_varatts = dict(temp_cel = dict(name='T2', offset=273.15),
                   rel_hum_pct = dict(name='RH',),
                   wind_spd_ms = dict(name='U2',),
                   wind_dir_deg = dict(name='U2_dir',),
                   precip_mm = dict(name='precip', scalefactor=24.), # convert hourly accumulation to daily
                   glb_rad_wm2 = dict(name='gndrad',),
                   cnr1_net_rad_total = dict(name='netrad'),
                   cnr1_sw_in = dict(name='DNSW'),
                   cnr1_sw_out = dict(name='UPSW'),
                   cnr1_lw_in_cor = dict(name='DNLW'),
                   cnr1_lw_out_cor = dict(name='UPLW'),
                   cnr1_lw_in_raw = dict(name='DNLW_raw'),
                   cnr1_lw_out_raw = dict(name='UPLW_raw'),
                   cnr1_temp_c = dict(name='Ts', offset=273.15), )
stn_readargs = dict(header=0, index_col=0, usecols=['timestamp_est'], parse_dates=True, na_values=['*','no data'])
minmax_vars = dict(T2=('Tmin','Tmax'), Ts=('TSmin','TSmax'), RH=('RHmin','RHmax'), Q2=('Q2min','Q2max'), 
                   precip=(None,'MaxPrecip_1h'), U2=(None,'U2max'))
meta = StationMeta(name='UTM', title='University of Toronto, Mississauga', region='Ontario',
                   filename='UTMMS Full Data Jan 1 2000 to Sept 26 2018.xlsx', testfile='UTM_test.xlsx',
                   readargs=stn_readargs, varatts=stn_varatts, minmax=minmax_vars, sampling='h')
ontario_station_list[meta.name] = meta


def getFolderFileName(station=None, region='Ontario', mode='daily'):
    ''' return folder and file name in standard format '''
    mode_str = mode
    mode_folder = 'stnavg'
    if mode.lower() == 'daily': mode_folder = 'station_daily'
    else: raise NotImplementedError(mode)
    folder = '{:s}/{:s}/{:s}/'.format(root_folder,region,mode_folder)
    filename = "{:s}_{:s}.nc".format(station,mode_str).lower()
    # return
    return folder,filename

## functions to load station data from source files


def loadStation_Src(station, region='Ontario', station_list=None, ldebug=False, varatts=varatts, **kwargs):
    ''' load station data from original source into pandas dataframe '''
    # get station meta data
    if station_list is None:
        station_list = globals()[region.lower()+'_station_list']
    station = station_list[station]
    # figure out read parameters
    if station.file_fmt == 'xls': readargs = dict() # default args
    readargs.update(station.readargs); readargs.update(kwargs)
    # add column/variables
    if 'usecols' in readargs: readargs['usecols'].extend(station.varatts.keys())
    else: readargs['usecols'] = station.varatts.keys()
    ## load file(s) in Pandas
    filelist = [station.testfile] if ldebug else station.filelist 
    df_list = []
    for filename in filelist:
        filepath = osp.join(station.folder,filename)
        if station.file_fmt == 'xls':
            if ldebug: print(readargs)
            df_list.append(pd.read_excel(filepath, **readargs))
        else:
            raise NotImplementedError(station.file_fmt)
    # join dataframes
    if len(df_list) == 1:
        df = df_list[0]
    else:
        raise NotImplementedError()
    # rename columns
    stn_varatts = station.varatts.copy()
    df = df.rename(columns={col:atts['name'] for col,atts in stn_varatts.items()}) # rename variables/columns
    df = df.rename_axis("time", axis="index") # rename axis/index to time
    ravmap = {atts['name']:col for col,atts in stn_varatts.items()}
    # compute water vapor pressure
    varlist = df.columns
    if ldebug: print(varlist)
    if 'Q2' not in varlist and 'T2' in varlist and 'RH' in varlist:
        lKelvin = stn_varatts[ravmap['T2']].get('offset',0) == 0
        df['Q2'] = e_sat(df['T2'], lKelvin=lKelvin) * df['RH']/100.
    ## aggregate to daily
    if station.sampling != 'D':
        rdf = df.resample('1D',)
        df = rdf.mean()
        # add min/max
        for var0,minmax in station.minmax.items():
            if var0 in df.columns:
                for mvar,mode in zip(minmax,('min','max')):
                    if mvar: # could be None if either min or max is not required
                        df[mvar] = getattr(rdf[var0],mode)() # compute min/max
                        # add new attributes (same as master var)
                        atts = stn_varatts[ravmap[var0]].copy() if var0 in ravmap else dict() 
                        atts['name'] = mvar
                        stn_varatts[mvar] = atts
    ## format dataframe
    for atts in stn_varatts.values():
        varname = atts['name']; sf = atts.get('scalefactor',1); of = atts.get('offset',0)
        if sf != 1: df[varname] = df[varname] * sf
        if of != 0: df[varname] = df[varname] + of
    # convert to xarray and add attributes
    xds = df.to_xarray()
    for varname,variable in xds.data_vars.items():
        if varname in varatts:
            variable.attrs.update(varatts[varname])
    xds.attrs['name'] = station.name
    xds.attrs['title'] = station.title
    xds.attrs['region'] = station.region
    
    # return properly formatted dataset
    return xds


## functions to load station data (from daily NetCDF files)

if __name__ == '__main__':
  
    import time
    print('pandas version:',pd.__version__)
  
#     mode = 'load_source'
    mode = 'convert_stations'
  
  
    if mode == 'load_source':
        
        xds = loadStation_Src(station='UTM', region='Ontario', ldebug=True,)
        
        print(xds)
        print(xds.attrs)
        print()
        var0 = next(iter(xds.data_vars.values()))
        print(var0)
        print(var0.attrs)
        
    elif mode == 'convert_stations':

        # start operation
        start = time.time()
        
        # load data        
        print("\nLoading time-varying data from source file\n")
        xds = loadStation_Src(station='UTM', region='Ontario', ldebug=False)
        print(xds)
        
        # write NetCDF
        nc_filepath = osp.join(*getFolderFileName(station=xds.attrs['name'], region=xds.attrs['region'], mode='daily'))
        xds.to_netcdf(nc_filepath)
        # add timestamp
        print("\nAdding human-readable time-stamp variable ('time_stamp')\n")
        ncds = nc.Dataset(nc_filepath, mode='a')
        ncts = addTimeStamps(ncds, units='day') # add time-stamps
        ncds.close()
        # print timing
        end = time.time()
        print(('\n   Required time:   {:.0f} seconds\n'.format(end-start)))

        
        
        