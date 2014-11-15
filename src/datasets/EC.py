# coding: utf-8
'''
Created on 2014-08-20

A module to load station daily data from Environment Canada from ASCII files and convert them to monthly 
NetCDF datasets (with extremes); the module also provides a wrapper to load the NetCDF datasets. 

@author: Andre R. Erler, GPL v3
'''

# external imports
import numpy as np
from copy import deepcopy
import codecs
import calendar
# internal imports
from datasets.common import days_per_month, name_of_month, data_root
from geodata.misc import ParseError, DateError, VariableError, ArgumentError, RecordClass, StrictRecordClass
from geodata.base import Axis, Variable, Dataset
from geodata.nctools import writeNetCDF
from geodata.netcdf import DatasetNetCDF
import average.derived_variables as dv
# from geodata.misc import DatasetError
from warnings import warn
#from Cython.Utility.MemoryView import tmpdata

## EC (Environment Canada) Meta-data

dataset_name = 'EC'
root_folder = data_root + dataset_name + '/'
orig_ts_file = '{0:s}{1:s}.txt' # filename pattern: variable name and station ID
tsfile = 'ec{0:s}_monthly.nc' # filename pattern: station type
tsfile_prov = 'ec{0:s}_{1:s}_monthly.nc' # filename pattern with province: station type, province  
avgfile = 'ec{0:s}_clim{1:s}.nc' # filename pattern: station type and ('_'+period)
avgfolder = root_folder + 'ecavg/'  # folder for user data

# variable attributes and name
varatts = dict(T2       = dict(name='T2', units='K', atts=dict(long_name='Average 2m Temperature')), # 2m average temperature
               Tmin     = dict(name='Tmin', units='K', atts=dict(long_name='Minimum 2m Temperature')), # 2m minimum temperature
               Tmax     = dict(name='Tmax', units='K', atts=dict(long_name='Maximum 2m Temperature')), # 2m maximum temperature
               precip   = dict(name='precip', units='kg/m^2/s', atts=dict(long_name='Total Precipitation')), # total precipitation
               solprec  = dict(name='solprec', units='kg/m^2/s', atts=dict(long_name='Solid Precipitation')), # solid precipitation
               liqprec  = dict(name='liqprec', units='kg/m^2/s', atts=dict(long_name='Liquid Precipitation')), # liquid precipitation
               # meta/constant data variables
               name    = dict(name='station_name', units='', atts=dict(long_name='Station Name')), # the proper name of the station
               prov    = dict(name='prov', units='', atts=dict(long_name='Province')), # in which Canadian Province the station is located
               joined  = dict(name='joined', units='', atts=dict(long_name='Joined Record or Single Station')), # whether or not the record contains more than one station 
               lat  = dict(name='lat', units='deg N', atts=dict(long_name='Latitude')), # geographic latitude field
               lon  = dict(name='lon', units='deg E', atts=dict(long_name='Longitude')), # geographic longitude field
               alt  = dict(name='zs', units='m', atts=dict(long_name='Station Elevation')), # station elevation
               begin_date = dict(name='begin_date', units='month', atts=dict(long_name='Month since 1979-01', # begin of station record
                                                                             description='Begin of Station Record (relative to 1979-01)')), 
               end_date = dict(name='end_date', units='month', atts=dict(long_name='Month since 1979-01', # begin of station record
                                                                         description='End of Station Record (relative to 1979-01)')),
               # axes (also sort of meta data)
               time     = dict(name='time', units='month', atts=dict(long_name='Month since 1979-01')), # time coordinate
               station  = dict(name='station', units='#', atts=dict(long_name='Station Number'))) # ordinal number of station
# list of variables to load
variable_list = varatts.keys() # also includes coordinate fields    


## a class that handles access to station records in ASCII files
class DailyStationRecord(StrictRecordClass):
  '''
    A class that is used by StationRecords to facilitate access to daily station records from ASCII files.  
  '''
  # list of station parameters that need to be supplied
  id         = '' # station ID
  name       = '' # station name
  variable   = '' # variable name (full name used in header)
  units      = '' # data units used in record
  dtype      = '' # data type (default: float32)
  missing    = '' # string indicating missing value
  flags      = '' # legal data flags (case sensitive)
  varmin     = 0. # smallest allowed value in data
  varmax     = 0. # largest allowed value in data
  filename   = '' # absolute path of ASCII file containing data record
  encoding   = '' # text file encoding
  prov       = '' # province (in Canada)  
  joined     = False # whether station record was merged with another nearby  
  begin_year = 0 # year of first record
  begin_mon  = 0 # month of first record
  end_year   = 0 # year of last record 
  end_mon    = 0 # month of last record
  lat        = 0. # latitude of station location
  lon        = 0. # longitude of station location
  alt        = 0. # station elevation (altitude)
  
  # id='', name='', datatype='', filename='', prov='', begin_year=0, begin_mon=0, end_year=0, end_mon=0, lat=0, lon=0
      
  def validateHeader(self, headerline):
    ''' validate header information against stored meta data '''
    # parse header line (print header if an error occurs)
    header = [elt.strip().lower() for elt in headerline.split(',')]
    if self.id.lower() != header[0]: raise ParseError, headerline # station ID
    if self.name.lower() != header[1]: raise ParseError, headerline # station name
    if self.prov.lower() != header[2]: raise ParseError, headerline # province
    if 'joined' not in header[3]: raise ParseError, headerline # station joined or not
    else:
      if self.joined and 'not' in header[3]: raise ParseError, headerline # station joined or not
      if not self.joined and 'not' not in header[3]: raise ParseError, headerline # station joined or not
    if 'daily' not in header[4]: raise ParseError, headerline # this class only deals with daily values
    if self.variable.lower() not in header[4]: raise ParseError, headerline # variable name
    if self.units.lower() not in header[5]: raise ParseError, headerline # variable units
    # if no error was raised, we are good
    
  def checkHeader(self):
    ''' open the station file and validate the header information; then close '''
    # open file
    f = codecs.open(self.filename, 'r', encoding=self.encoding)
    self.validateHeader(f.readline()) # read first line as header
    f.close()
  
  def parseRecord(self):
    ''' open the station file and parse records; return a daiy time-series '''
    # open file
    f = codecs.open(self.filename, 'r', encoding=self.encoding)
    self.validateHeader(f.readline()) # read first line as header
    # allocate daily data array (31 days per month, filled with NaN for missing values)
    tlen = ( (self.end_year - self.begin_year) * 12 + (self.end_mon - self.begin_mon +1) ) * 31
    data = np.empty((tlen,), dtype=self.dtype); data.fill(np.NaN) # use NaN as missing values
    # some stuff to remember
    lfloat = 'float' in self.dtype; lint = 'int' in self.dtype; lm = len(self.missing)
    # iterate over line
    oldyear = self.begin_year; oldmon = self.begin_mon -1; z = 0
    for line in f:      
      ll = line.replace('-9999.9', ' -9999.9').split() # without the replace, the split doesn't work      
      if ll[0].isdigit() and ll[1].isdigit():
        year = int(ll[0]); mon = int(ll[1])
        # check continuity
        if year == oldyear and mon == oldmon+1: pass
        elif year == oldyear+1 and oldmon == 12 and mon ==1: pass 
        else: raise DateError, line
        oldyear = year; oldmon = mon
#         # rigorous check of date bounds
#         if year == self.begin_year and mon < self.begin_mon: raise DateError, line
#         elif year < self.begin_year: raise DateError, line
#         if year == self.end_year and mon > self.end_mon: raise DateError, line
#         elif year > self.end_year: raise DateError, line
        # skip dates outside the specified begin/end dates
        if year < self.begin_year or year > self.end_year: pass # outside range 
        elif year == self.begin_year and mon < self.begin_mon: pass # start later
        elif year == self.end_year and mon > self.end_mon: pass # basically done
        else: # else we can proceed
          assert len(ll) < 34, line
          zz = z 
          # loop over daily values
          for num in ll[2:]:
            # evaluate daily value
            assert zz < data.size, line              
            if num[:lm] == self.missing: pass # missing value; already pre-filled NaN;  or num[-1] == 'M'
            else:
              if lfloat and '.' in num and 1 < len(num): # at least 1 digit plus decimal, i.e. ignore the flag
                if num[-1].isdigit(): n = float(num)
                elif num[-2].isdigit() and num[-1] in self.flags: n = float(num[:-1]) # remove data flag
                else: raise ParseError, "Unable to process value '{:s}' in line:\n {:s}".format(num,line)
              elif lint and 0 < len(num):# almost the same as for floats
                if num[-1].isdigit(): n = int(num)
                elif num[-2].isdigit() and num[-1] in self.flags: n = int(num[:-1]) # remove data flag
                else: raise ParseError, "Unable to process value '{:s}' in line:\n {:s}".format(num,line)
              else: raise ParseError, "Unable to process value '{:s}' in line:\n {:s}".format(num,line)
              if n < self.varmin: warn("Encountered value '{:s}' below minimum in line (ignored):\n {:s}".format(num,line))
              elif n > self.varmax: warn("Encountered value '{:s}' above maximum in line (ignored):\n {:s}".format(num,line))
              else: data[zz] = n # only now, we can accept the value
            # increment daily counter
            zz += 1 # here each month has 31 days (padded with missing values)
          if zz != z+31: raise ParseError, 'Line has {:d} values instead of 31:\n {:s}'.format(zz-z,line)  
          # increment counter
          z += 31
      elif ll[0] != 'Year' or ll[1] != 'Mo':
        raise ParseError, "No valid title or data found at begining of file:\n {:s}".format(self.filename)
    if z < tlen: raise ParseError, 'Reached end of file before specified end date: {:s}'.format(self.filename)    
    f.close() # close again
    # return array
    return data
  

## class that defines variable properties (specifics are implemented in children)
class VarDef(RecordClass):
  # variable specific
  name     = '' # full variable name (used in source files)
  atts     = None # dictionary with PyGeoData variable attributes
  prefix   = '' # file prefix for source file name (used with station ID)
  fileext  = '.txt' # file name extension (used for source files)
  # type specific
  datatype = '' # defined in child class; type of source file
  units    = '' # units used for data  (in source files)  
  dtype    = 'float32' # data type used for data
  encoding = 'UTF-8' # file encoding (used in source files)
  missing  = '' # string indicating missing value
  flags    = '' # legal data flags (case sensitive)
  varmin   = 0. # smallest allowed value in data
  varmax   = 0. # largest allowed value in data
  # inferred variables
  variable = '' # alias for name
  filepath = '' # inferred from prefix
  
  def __init__(self, **kwargs):
    super(VarDef,self).__init__(**kwargs)
    self.variable = self.name
    self.filepath = '{0:s}/{0:s}{1:s}{2:s}'.format(self.prefix,'{:s}',self.fileext)
    
  def convert(self, data): return data # needs to be implemented by child
  
  def getKWargs(self, *args):
    ''' Return a dictionary with the specified arguments and their values '''
    if len(args) == 0: 
      args = ['variable', 'units', 'varmin', 'varmax', 'missing', 'flags', 'dtype', 'encoding']
    kwargs = dict()
    for arg in args:
      kwargs[arg] = getattr(self,arg)
    return kwargs
  
# definition for precipitation files
class PrecipDef(VarDef):
  units    = 'mm'
  datatype = 'precip'
  title    = 'EC Precipitation Records'
  missing  = '-9999.99' # string indicating missing value (apparently not all have 'M'...)
  flags    = 'TEFACLXYZ' # legal data flags (case sensitive; 'M' for missing should be screened earlier)
  varmin   = 0. # smallest allowed value in data
  varmax   = 1.e3 # largest allowed value in data
  
# definition for temperature files
class TempDef(VarDef):
  units    = u'°C'
  datatype = 'temp'
  title    = 'EC Temperature Records'
  encoding = 'ISO-8859-15' # for some reason temperature files have a strange encodign scheme...
  missing  = '-9999.9' # string indicating missing value
  flags    = 'Ea' # legal data flags (case sensitive; 'M' for missing should be screened earlier)
  varmin   = -100. # smallest allowed value in data
  varmax   = 100. # largest allowed value in data
  
  def convert(self, data): return data + 273.15 # convert to Kelvin

## variable definitions for EC datasets

# daily precipitation variables in data
precip_vars = dict(precip=PrecipDef(name='precipitation', prefix='dt', atts=varatts['precip']),
                   solprec=PrecipDef(name='snowfall', prefix='ds', atts=varatts['solprec']),
                   liqprec=PrecipDef(name='rainfall', prefix='dr', atts=varatts['liqprec']))
# precipitation extremes (and other derived variables)
precip_xtrm = [dv.WetDays(),]
for var in precip_vars:
  for mode in ('min','max'):
    # ordinary & interval extrema: var, mode, [interval=7,] name=None, dimmap=None
    precip_xtrm.append(dict(var=var, mode=mode, klass=dv.Extrema))      
    precip_xtrm.append(dict(var=var, mode=mode, interval=7, klass=dv.MeanExtrema))      
# consecutive events: var, mode, threshold=0, name=None, longname=None, dimmap=None
tmpatts = dict(var='precip', threshold=2.3e-7, klass=dv.ConsecutiveExtrema)
precip_xtrm.append(dict(name='CWD', mode='above', longname='Consecutive Wet Days', **tmpatts))
precip_xtrm.append(dict(name='CDD', mode='below', longname='Consecutive Dry Days', **tmpatts))

# daily temperature variables in data
temp_vars   = dict(T2=TempDef(name='mean temperature', prefix='dm', atts=varatts['T2']),
                   Tmin=TempDef(name='minimum temperature', prefix='dn', atts=varatts['Tmin']),
                   Tmax=TempDef(name='maximum temperature', prefix='dx', atts=varatts['Tmax']))  
# temperature extremes (and other derived variables)
temp_xtrm   = [dv.FrostDays(),]
for var in temp_vars:
  for mode in ('min','max'):
    # ordinary & interval extrema: var, mode, [interval=7,] name=None, dimmap=None
    temp_xtrm.append(dict(var=var, mode=mode, klass=dv.Extrema))      
    temp_xtrm.append(dict(var=var, mode=mode, interval=7, klass=dv.MeanExtrema))      
# consecutive events: var, mode, threshold=0, name=None, longname=None, dimmap=None
tmpatts = dict(mode='below', threshold=273.15, klass=dv.ConsecutiveExtrema) # threshold after conversion
temp_xtrm.append(dict(name='CFD', var='T2', longname='Consecutive Frost Days', **tmpatts))
temp_xtrm.append(dict(name='CDFD', var='Tmax', longname='Consecutive Day Frost Days', **tmpatts))
temp_xtrm.append(dict(name='CNFD', var='Tmin', longname='Consecutive Night Frost Days', **tmpatts))

# map from common variable names to WRF names (which are used in the derived_variables module)
ec_varmap = dict(RAIN='precip', south_north='time', time='station', west_east=None, # swap order of axes
                 T2MIN='Tmin', T2MAX='Tmax', ) 

# definition of station meta data format 
ec_header_format = ('No','StnId','Prov','From','To','Lat(deg)','Long(deg)','Elev(m)','Joined','Station','name') 
ec_station_format = tuple([(None, int), 
                          ('id', str),                            
                          ('prov', str),                        
                          ('begin_year', int),     
                          ('begin_mon', int),
                          ('end_year', int),   
                          ('end_mon', int),
                          ('lat', float),                     
                          ('lon', float),                       
                          ('alt', float),                       
                          ('joined', lambda l: l.upper() == 'Y'),
                          ('name', str),])
    
## class to read station records and return a dataset
class StationRecords(object):
  '''
    A class that provides methods to load station data and associated meta data from files of a given format;
    The format itself will be defines in child classes.
    The data will be converted to monthly statistics and accessible as a PyGeoData dataset or can be written 
    to a NetCDF file.
  '''
  # arguments
  folder      = '' # root folder for station data: interval and datatype (source folder)
  stationfile = 'stations.txt' # file that contains station meta data (to load station records)
  encoding    = '' # encoding of station file
  interval    = '' # source data interval (currently only daily)
  datatype    = '' # variable class, e.g. temperature or precipitation types
  title       = '' # dataset title
  variables   = None # parameters and definitions associated with variables
  extremes    = None # list of derived/extreme variables to be computed as well
  atts        = None # attributes of resulting dataset (including name and title)
  header_format  = '' # station format definition (for validation)
  station_format = '' # station format definition (for reading)
  constraints    = None # constraints to limit the number of stations that are loaded
  # internal variables
  stationlists   = None # list of station objects
  dataset        = None # PyGeoData Dataset (will hold results) 
  
  def __init__(self, folder='', stationfile='stations.txt', variables=None, extremes=None, interval='daily', 
               encoding='', header_format=None, station_format=None, constraints=None, atts=None, varmap=None):
    ''' Parse station file and initialize station records. '''
    # some input checks
    if not isinstance(stationfile,basestring): raise TypeError
    if interval != 'daily': raise NotImplementedError
    if header_format is None: header_format = ec_header_format # default
    elif not isinstance(header_format,(tuple,list)): raise TypeError
    if station_format is None: station_format = ec_station_format # default    
    elif not isinstance(station_format,(tuple,list)): raise TypeError
    if not isinstance(constraints,dict) and constraints is not None: raise TypeError
    # variables et al.
    if not isinstance(variables,dict): raise TypeError
    datatype = variables.values()[0].datatype; title = variables.values()[0].title;
    if not all([var.datatype == datatype for var in variables.values()]): raise VariableError
    if not all([var.title == title for var in variables.values()]): raise VariableError
    if extremes is None and datatype == 'precip': extremes = deepcopy(precip_xtrm)
    elif extremes is None and datatype == 'temp': extremes = deepcopy(temp_xtrm)
    # N.B.: need to use deepcopy, because we are modifying the objects      
    elif not isinstance(extremes,(list,tuple)): raise TypeError
    if varmap is None: varmap = ec_varmap
    elif not isinstance(varmap, dict): raise TypeError
    # misc
    encoding = encoding or variables.values()[0].encoding 
    if atts is None: atts = dict(name=datatype, title=title) # default name
    elif not isinstance(atts,dict): raise TypeError # resulting dataset attributes
    if not isinstance(encoding,basestring): raise TypeError
    folder = folder or '{:s}/{:s}_{:s}/'.format(root_folder,interval,datatype) # default folder scheme 
    if not isinstance(folder,basestring): raise TypeError
    # save arguments
    self.folder = folder
    self.stationfile = stationfile
    self.encoding = encoding
    self.interval = interval
    self.datatype = datatype
    self.title = title
    self.variables = variables
    self.extremes = extremes
    self.varmap = varmap
    self.ravmap = dict((value,key) for key,value in varmap.iteritems() if value is not None) # reverse var map
    self.atts = atts
    self.header_format = header_format
    self.station_format = station_format
    self.constraints = constraints
    ## initialize station objects from file
    # open and parse station file
    stationfile = '{:s}/{:s}'.format(folder,stationfile)
    f = codecs.open(stationfile, 'r', encoding=encoding)
    # initialize station objects and add to list
    header = f.readline() # read first line of header (title)
    if not datatype.lower() in header.lower(): raise ParseError
    f.readline() # discard second line (French)
    header = f.readline() # read third line (column definitions)
    for key,col in zip(header_format,header.split()):
      if key.lower() != col.lower(): 
        raise ParseError, "Column headers do not match format specification: {:s} != {:s} \n {:s}".format(key,col,header)
    f.readline() # discard forth line (French)    
    # initialize station list
    self.stationlists = {varname:[] for varname in variables.iterkeys()} # a separate list for each variable
    z = 0 # row counter 
    ns = 0 # station counter
    # loop over lines (each defiens a station)
    for line in f:
      z += 1 # increment counter
      collist = line.split()
      if len(collist) > 0: # skip empty lines
        stdef = dict() # station specific arguments to instantiate station object
        # loop over column titles
        zz = 0 # column counter
        for key,fct in station_format[:-1]: # loop over columns
          if key is None: # None means skip this column
            if zz == 0: # first column
              if z != fct(collist[zz]): raise ParseError, "Station number is not consistent with line count:\n {:s}".format(line)
          else:
            stdef[key] = fct(collist[zz]) # convert value and assign to argument
          zz += 1 # increment column
        assert zz <= len(collist) # not done yet
        # collect all remaining elements
        key,fct = station_format[-1]
        stdef[key] = fct(' '.join(collist[zz:]))
        # check station constraints
        if constraints is None: ladd = True
        else:
          ladd = True
          for key,val in constraints.iteritems():
            if stdef[key] not in val: ladd = False
        # instantiate station objects for each variable and append to lists
        if ladd:
          ns += 1
          # loop over variable definitions
          for varname,vardef in variables.iteritems():
            filename = '{0:s}/{1:s}'.format(folder,vardef.filepath.format(stdef['id']))
            kwargs = dict() # combine station and variable attributes
            kwargs.update(stdef); kwargs.update(vardef.getKWargs())
            station = DailyStationRecord(filename=filename, **kwargs)
            station.checkHeader() 
            self.stationlists[varname].append(station)
    assert len(self.stationlists[varname]) == ns # make sure we got all (lists should have the same length)
    
  def prepareDataset(self, filename=None, folder=None):
    ''' prepare a PyGeoData dataset for the station data (with all the meta data); 
        create a NetCDF file for monthly data; also add derived variables          '''
    if folder is None: folder = '{:s}/ecavg/'.format(root_folder) # default folder scheme 
    elif not isinstance(folder,basestring): raise TypeError
    if filename is None: filename = 'ec{:s}_monthly.nc'.format(self.datatype) # default folder scheme 
    elif not isinstance(filename,basestring): raise TypeError
    # meta data arrays
    dataset = Dataset(atts=self.atts)
    # station axis (by ordinal number)
    stationlist = self.stationlists.values()[0] # just use first list, since meta data is the same
    assert all([len(stationlist) == len(stnlst) for stnlst in self.stationlists.values()]) # make sure none is missing
    station = Axis(coord=np.arange(1,len(stationlist)+1, dtype='int16'), **varatts['station']) # start at 1
    # station name
    namelen = max([len(stn.name) for stn in stationlist])
    strarray = np.array([stn.name.ljust(namelen) for stn in stationlist], dtype='|S{:d}'.format(namelen))
    dataset += Variable(axes=(station,), data=strarray, **varatts['name'])
    # station province
    strarray = np.array([stn.prov for stn in stationlist], dtype='|S2') # always two letters
    dataset += Variable(axes=(station,), data=strarray, **varatts['prov'])
    # station joined
    boolarray = np.array([stn.joined for stn in stationlist], dtype='bool') # boolean
    dataset += Variable(axes=(station,), data=boolarray, **varatts['joined'])
    # geo locators (lat/lon/alt)
    for coord in ('lat','lon','alt'):
      coordarray = np.array([getattr(stn,coord) for stn in stationlist], dtype='float32') # single precision float
      dataset += Variable(axes=(station,), data=coordarray, **varatts[coord])
    # start/end dates (month relative to 1979-01)
    for pnt in ('begin','end'):
      yeararray = np.array([getattr(stn,pnt+'_year') for stn in stationlist], dtype='int16') # single precision integer
      monarray = np.array([getattr(stn,pnt+'_mon') for stn in stationlist], dtype='int16') # single precision integer
      datearray = ( yeararray - 1979 )*12 + monarray - 1  # compute month relative to 1979-01
      dataset += Variable(axes=(station,), data=datearray, **varatts[pnt+'_date'])
      # save bounds to determine size of time dimension
      if pnt == 'begin': begin_date = np.min(datearray) 
      elif pnt == 'end': end_date = np.max(datearray) 
    # add variables for monthly values
    time = Axis(coord=np.arange(begin_date, end_date+1, dtype='int16'), **varatts['time'])
    # loop over variables
    for vardef in self.variables.itervalues():
      dataset += Variable(axes=(station,time), dtype=vardef.dtype, **vardef.atts)
    # write dataset to file
    ncfile = '{:s}/{:s}'.format(folder,filename)      
    #zlib = dict(chunksizes=dict(station=len(station))) # compression settings; probably OK as is 
    ncset = writeNetCDF(dataset, ncfile, feedback=False, overwrite=True, writeData=True, 
                        skipUnloaded=True, close=False, zlib=True)
    # add derived variables
    extremes = []
    for xvar in self.extremes:
      if isinstance(xvar,dict):
        var = xvar.pop('var'); mode = xvar.pop('mode'); Klass = xvar.pop('klass')
        xvar = Klass(ncset.variables[var], mode, **xvar)
        xvar.prerequisites = [self.ravmap.get(varname,varname) for varname in xvar.prerequisites]
      elif isinstance(xvar,dv.DerivedVariable):
        xvar.axes = tuple([self.varmap.get(varname,varname) for varname in xvar.axes if self.varmap.get(varname,varname)])
      else: raise TypeError
      # adapt variable instances for this dataset (i.e. axes and dependencies)
      xvar.normalize = False # different aggregation
      # check axes
      if len(xvar.axes) != 2 or xvar.axes != (varatts['station']['name'], varatts['time']['name']):
        print xvar.axes
        raise dv.DerivedVariableError, "Axes ('station', 'time') are required; adjust varmap as needed."
      # finalize
      xvar.checkPrerequisites(ncset, const=None, varmap=self.varmap)
      xvar.createVariable(ncset)
      extremes.append(xvar)
    self.extremes = extremes
    # reopen netcdf file with netcdf dataset
    self.dataset = DatasetNetCDF(dataset=ncset, mode='rw', load=True) # always need to specify mode manually
    
  def readStationData(self):
    ''' read station data from source files and store in dataset '''
    assert self.dataset
    # determine record begin and end indices
    all_begin = self.dataset.time.coord[0] # coordinate value of first time step
    begin_idx = ( self.dataset.begin_date.getArray() - all_begin ) * 31.
    end_idx = ( self.dataset.end_date.getArray() - all_begin + 1 ) * 31.
    # loop over variables
    dailydata = dict() # to store daily data for derived variables
    monlydata = dict() # monthly data, but transposed
    ravmap = self.ravmap # shortcut for convenience  
    print("\n   ***   Preparing {:s}   ***\n   Constraints: {:s}\n".format(self.title,str(self.constraints)))
    for var,vardef in self.variables.iteritems():
      print("\n {:s} ('{:s}'):\n".format(vardef.name.title(),var))
      varobj = self.dataset[var] # get variable object
      wrfvar = ravmap.get(varobj.name,varobj.name)
      # allocate array
      shape = (varobj.shape[0], varobj.shape[1]*31) # daily data!
      dailytmp = np.empty(shape, dtype=varobj.dtype); dailytmp.fill(np.NaN) # initialize all with NaN
      # loop over stations
      s = 0 # station counter
      for station in self.stationlists[var]:
        print("   {:<15s} {:s}".format(station.name,station.filename))
        # read station file
        dailytmp[s,begin_idx[s]:end_idx[s]] = station.parseRecord()  
        s += 1 # next station
      assert s == varobj.shape[0]
      dailytmp = vardef.convert(dailytmp) # apply conversion function
      # compute monthly average
      dailytmp = dailytmp.reshape(varobj.shape+(31,))
      monlytmp = np.nanmean(dailytmp,axis=-1) # squeezes automatically
      # store daily and monthly data for computation of derived variables
      dailydata[wrfvar] = dailytmp
      monlydata[wrfvar] = monlytmp
      # load data      
      varobj.load(monlytmp); varobj.sync()
      del dailytmp, monlytmp
    # loop over derived nonlinear variables/extremes
    if any(not var.linear for var in self.extremes): print('\n computing (nonlinear) daily variables:')
    for var in self.extremes:      
      if not var.linear:
        print("   {:<15s} {:s}".format(var.name,str(tuple(self.varmap.get(varname,varname) for varname in var.prerequisites))))
        varobj = self.dataset[var.name] # get variable object
        if var.name not in ravmap: ravmap[var.name] = var.name # naming convention for tmp storage 
        wrfvar = ravmap[var.name] 
        # allocate memory for monthly values
        tmp = np.ma.empty(varobj.shape, dtype=varobj.dtype); tmp.fill(np.NaN) 
        # N.B.: some derived variable types may return masked arrays
        monlydata[wrfvar] = tmp
    # loop over time steps      
    tmpvars = dict()
    for m,mon in enumerate(varobj.axes[1].coord):
      # figure out length of month
      if mon%12 == 1: # February
        if calendar.isleap(1979 + mon/12): lmon = 29
        else: lmon = 28
      else: lmon = days_per_month[mon%12]
      # construct arrays for this month
      tmpdata = {varname:data[:,m,0:lmon] for varname,data in dailydata.iteritems()}      
      for var in self.extremes:      
        if not var.linear:
          varobj = self.dataset[var.name] # get variable object
          wrfvar = ravmap[var.name]
          dailytmp = var.computeValues(tmpdata, aggax=1, delta=86400., tmp=tmpvars, ignoreNaN=True)        
          tmpdata[wrfvar] = dailytmp
          monlytmp = var.aggregateValues(dailytmp, aggdata=None, aggax=1, ignoreNaN=True) # last axis
          assert monlytmp.shape == (s,)
          monlydata[wrfvar][:,m] = monlytmp
    # loop over linear derived variables/extremes
    if any(var.linear for var in self.extremes): print('\n computing (linear) monthly variables:')
    for var in self.extremes:      
      varobj = self.dataset[var.name] # get variable object
      wrfvar = ravmap[var.name]
      if var.linear:
        # compute from available monthly data
        print("   {:<15s} {:s}".format(var.name,str(tuple(self.varmap.get(varname,varname) for varname in var.prerequisites))))
        monlytmp = var.computeValues(monlydata, aggax=1, delta=86400., ignoreNaN=True)
        monlydata[wrfvar] = monlytmp
      tmpload = monlydata[wrfvar]
      assert varobj.shape == tmpload.shape
      varobj.load(tmpload); varobj.sync()
        
    

## load pre-processed EC station time-series
def loadEC_TS(name=None, filetype=None, prov=None, varlist=None, varatts=None, filelist=None, folder=None): 
  ''' Load a monthly time-series of pre-processed EC station data. '''
  if filetype is None: raise ArgumentError, "A 'filetype' needs to be specified ('temp' or 'precip')."
  elif not filetype in ('temp','precip'): raise ArgumentError
  if name is None: name = 'ec{:s}'.format(filetype) # prepend ec to the filetype
  if prov is not None and not isinstance(prov,basestring): raise TypeError
  if folder is None: folder = avgfolder
  if filelist is None:
    if prov: filelist = [tsfile_prov.format(filetype, prov)]
    else: filelist = [tsfile.format(filetype)]
  # open NetCDF file (name, varlist, and varatts are passed on directly)
  dataset = DatasetNetCDF(name=name, folder=folder, filelist=filelist, varlist=varlist, varatts=varatts, 
                            multifile=False, ncformat='NETCDF4')
  return dataset
loadEC_StnTS = loadEC_TS # just an alias

## load pre-processed EC station climatology
def loadEC(): 
  ''' Load a pre-processed EC station climatology. '''
  return NotImplementedError
  
## Dataset API

dataset_name # dataset name
root_folder # root folder of the dataset
orig_file_pattern = orig_ts_file # filename pattern: variable name and resolution
ts_file_pattern = tsfile # filename pattern: grid
clim_file_pattern = avgfile # filename pattern: variable name and resolution
data_folder = avgfolder # folder for user data
grid_def = None # no grid here...
LTM_grids = None 
TS_grids = None
grid_res = None
default_grid = None
# functions to access specific datasets
loadLongTermMean = None # climatology provided by publisher
loadTimeSeries = None # time-series data
loadClimatology = None # pre-processed, standardized climatology
loadStationTimeSeries = loadEC_TS # time-series data
loadStationClimatology = loadEC # pre-processed, standardized climatology

if __name__ == '__main__':

#   mode = 'test_station_object'
#   mode = 'test_station_reader'
#   mode = 'test_conversion'
#   mode = 'convert_all_stations'
#   mode = 'convert_prov_stations'
  mode = 'test_timeseries'
  
  # test wrapper function to load time series data from EC stations
  if mode == 'test_timeseries':
    
    # load averaged climatology file
    print('')
    dataset = loadEC_TS(filetype='precip', prov='NL')
    print(dataset)
    print('')
    print(dataset.station)
    print(dataset.time)
    print(dataset.time.coord)
    print(dataset.time.coord[105*12]) # Jan 1979, the origin of time...

        
  # test station object initialization
  elif mode == 'test_station_object':  
    
    # initialize station (new way with VarDef)
#     var = PrecipDef(name='precipitation', prefix='dt', atts=varatts['precip'])
#     test = DailyStationRecord(id='250M001', name='MOULD BAY', filename='/data/EC/daily_precip/dt/dt250M001.txt',  
#                               begin_year=1948, begin_mon=1, end_year=2007, end_mon=11, prov='NT', joined=True, 
#                               lat=76.2, lon=-119.3, alt=2, **var.getKWargs())    
    var = TempDef(name='maximum temperature', prefix='dx', atts=varatts['Tmax'])
    test = DailyStationRecord(id='5010640', name='CYPRESS RIVER', filename='/data/EC/daily_temp/dx/dx5010640.txt',
                              begin_year=1949, begin_mon=1, end_year=2012, end_mon=3, prov='MB', joined=False, 
                              lat=49.55, lon=-99.08, alt=374, **var.getKWargs())
    test.checkHeader() # fail early...
    data = var.convert(test.parseRecord())    
    print data.shape, data.dtype
    print np.nanmin(data), np.nanmean(data), np.nanmax(data)
  
  
  # tests station reader initialization
  elif mode == 'test_station_reader':
    
    # prepare input
    variables = temp_vars
    # initialize station record container (PE only has 3 stations - ideal for testing!)
    test = StationRecords(folder='', variables=variables, constraints=dict(prov=('PE',)))
    # show dataset
    test.prepareDataset()
    print test.dataset
    print('')
    # write to netcdf file
    test.writeDataset(filename='test.nc', folder='/home/data/', feedback=False, 
                      overwrite=True, writeData=True, skipUnloaded=True)
    print('')
    # test netcdf file
    dataset = DatasetNetCDF(filelist=['/home/data/test.nc'])
    print dataset
    print('')
    print dataset.station_name[1:,] # test string variable recall
    
  
  # tests entire conversion process
  elif mode == 'test_conversion':
    
    prov = 'BC'
    # prepare input
    variables = temp_vars #dict(T2=temp_vars['T2'])
#     variables = precip_vars #dict(precip=temp_vars['precip'])
    # initialize station record container (PE only has 3 stations - ideal for testing!)
    test = StationRecords(folder='', variables=variables, constraints=dict(prov=(prov,)))
    # create netcdf file
    print('')
    filename = tsfile_prov.format(variables.values()[0].datatype,prov)        
    test.prepareDataset(filename=filename, folder=None)
    # read actual station data
    test.readStationData()
    dataset = test.dataset
    print('')
    print(dataset)
    print('\n')
    for varname,var in dataset.variables.iteritems():
      if var.hasAxis('time') and var.hasAxis('station'):
        data = var.getArray()
        if 'precip' in variables:
          print('{:>14s}: {:5.2f} | {:5.2f} | {:5.2f}'.format(
                var.name, np.nanmin(data), np.nanmean(data), np.nanmax(data))) 
        else: 
          print('{:>10s}: {:5.1f} | {:5.1f} | {:5.1f}'.format(
                var.name, np.nanmin(data), np.nanmean(data), np.nanmax(data)))
  
  
  # convert provincial station date to NetCDF
  elif mode == 'convert_prov_stations':
    
    # loop over provinces
    for prov in ('BC', 'YT', 'NT', 'NU', 'AB', 'SK', 'MB', 'ON', 'QC', 'NB', 'NS', 'PE', 'NL'):
      # loop over variable types
      for variables in (precip_vars, temp_vars,): # precip_vars, temp_vars,
        
        # initialize station record container
        stations = StationRecords(variables=variables, constraints=dict(prov=(prov,)))
        # create netcdf file (one per province)
        filename = tsfile_prov.format(variables.values()[0].datatype,prov)        
        stations.prepareDataset(filename=filename, folder=None)
        # read actual station data
        stations.readStationData()


  # convert all station date to NetCDF
  elif mode == 'convert_all_stations':
    
    # loop over variable types
    for variables in (precip_vars, temp_vars,): # precip_vars, temp_vars,
      
      # initialize station record container
      stations = StationRecords(variables=variables, constraints=None)
      # create netcdf file
      stations.prepareDataset(filename=None, folder=None) # default settings
      # read actual station data
      stations.readStationData()
      
      