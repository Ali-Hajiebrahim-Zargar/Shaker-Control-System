import csv
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('viewer',ROOT/'shaker_csv_viewer.py')
v=importlib.util.module_from_spec(spec);spec.loader.exec_module(v)

class Parsing(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.folder=Path(self.tmp.name)
 def tearDown(self): self.tmp.cleanup()
 def csv(self,name,header,rows,delimiter=',',preamble=''):
  p=self.folder/name
  with p.open('w',encoding='utf-8-sig',newline='') as f:
   f.write(preamble);w=csv.writer(f,delimiter=delimiter);w.writerow(header);w.writerows(rows)
  return p
 def test_force_preserves_saved_newtons_and_tare(self):
  rows=[[i/100,255+i,12.5+i/10,100-i,'POSITION_HOLD'] for i in range(100)]
  p=self.csv('force.csv',['Time (s)','Raw Counts','Force (N)','Motor Position (counts)','Motor State'],rows)
  doc=v.load_csv(p,sensor_rating='not used',baseline_counts='not used')
  self.assertEqual(doc['records'][0]['y'],[r[2] for r in rows]);self.assertEqual(len(doc['records']),3)
  self.assertEqual(doc['records'][1]['y'],[r[3] for r in rows]);self.assertEqual(doc['records'][2]['y'],[r[1] for r in rows])
  self.assertEqual(doc['records'][0]['states'][0],'POSITION_HOLD')
 def test_scope_separates_repeated_time_channels_and_rounded_capture_ids(self):
  rows=[]
  for cap,ch,bias in [(1,'CH1',0),(2,'CH1',2),(2,'CH2',4),(2,'CH2',6)]:
   rows.extend([[cap,ch,i,(i-5)/100,bias+np.sin(i)] for i in range(20)])
  p=self.csv('scope.csv',['Capture Time (s)','Channel','Sample Index','Waveform Time (s)','Voltage (V)'],rows)
  doc=v.load_csv(p)
  self.assertEqual(len(doc['records']),4)
  for i,r in enumerate(doc['records']):
   self.assertEqual(len(r['t']),20);self.assertEqual(r['t'][0],-.05);self.assertAlmostEqual(r['y'][0],i*2)
 def test_count_conversion_and_optional_legacy_tare(self):
  p=self.csv('force.csv',['Time','Counts'],[[i/100,767] for i in range(100)])
  self.assertEqual(v.load_csv(p)['records'][0]['y'][0],45)
  self.assertEqual(v.load_csv(p,baseline_mode='First 1 s median (unloaded only)')['records'][0]['y'][0],0)
  with self.assertRaises(ValueError):v.load_csv(p,sensor_rating=-45)
  with self.assertRaises(ValueError):v.load_csv(p,sensor_rating=float('nan'))
 def test_legacy_scope_ragged_metadata(self):
  p=self.folder/'tek.csv';p.write_text('Record Length,100\nSource,CH2\n'+''.join(f',,,{i/100},{np.sin(i)}\n' for i in range(100)))
  doc=v.load_csv(p);self.assertEqual(doc['records'][0]['channel'],'CH2');self.assertEqual(len(doc['records'][0]['t']),100)
 def test_old_combined_schema(self):
  p=self.csv('old.csv',['time_s','piezo_voltage_V','force_N','position_counts','motor_state'],[[i/100,-.261,1+i,2*i,'BRAKED'] for i in range(20)])
  doc=v.load_csv(p);self.assertEqual(len(doc['records']),3);self.assertIn('constant',doc['records'][1]['note'])
 def test_bom_preamble_semicolon_decimal_comma(self):
  p=self.csv('europe.csv',['Time (s)','Force (N)'],[[str(i/10).replace('.',','),'1,5'] for i in range(20)],';', 'Laboratory export\n')
  r=v.load_csv(p)['records'][0];self.assertEqual(r['y'],[1.5]*20);self.assertAlmostEqual(r['t'][1],.1)
 def test_missing_values_and_duplicates(self):
  rows=[[i/100,i] for i in range(20)];rows.insert(3,[.02,99]);rows.insert(12,[.115,''])
  p=self.csv('force.csv',['Time (s)','Force (N)'],rows)
  r=v.load_csv(p)['records'][0];self.assertEqual(len(r['t']),20);self.assertEqual(r['y'][2],99);self.assertIn('duplicates',r['note']);self.assertIn('Omitted 1',r['note'])
 def test_no_silent_merging_restarted_time(self):
  p=self.csv('force.csv',['Time (s)','Force (N)'],[[i%10,1] for i in range(20)])
  with self.assertRaisesRegex(ValueError,'backwards'):v.load_csv(p)
 def test_empty_and_wrong_format(self):
  p=self.csv('empty.csv',['Time (s)','Force (N)'],[])
  with self.assertRaisesRegex(ValueError,'no recorded samples'):v.load_csv(p)
  p=self.csv('force.csv',['Time (s)','Force (N)'],[[i,1] for i in range(20)])
  with self.assertRaisesRegex(ValueError,'Detected'):v.load_csv(p,'Scope project CSV')
 def test_scope_invalid_ids_and_partial_capture(self):
  header=['Capture Time (s)','Channel','Sample Index','Waveform Time (s)','Voltage (V)']
  p=self.csv('scope.csv',header,[[1,'CH1',i,i/100,i] for i in range(20)]+[[2,'CH1',i,i/100,i] for i in range(5)])
  doc=v.load_csv(p);self.assertEqual(len(doc['records']),1);self.assertEqual(len(doc['warnings']),1)
  p=self.csv('bad.csv',header,[[1,'',i,i/100,i] for i in range(20)])
  with self.assertRaisesRegex(ValueError,'missing or invalid'):v.load_csv(p)
 def test_large_file_no_sample_loss(self):
  r=v.make_record(np.arange(150000)*.001,np.sin(np.arange(150000)*.01),'large','V',.02)
  self.assertEqual(len(r['t']),150000)

if __name__=='__main__':unittest.main(verbosity=2)
