from pathlib import Path
import argparse,struct,zipfile,xml.etree.ElementTree as ET
parser=argparse.ArgumentParser()
parser.add_argument("apk")
parser.add_argument("api_versions")
args=parser.parse_args()
apk=Path(args.apk)
d=zipfile.ZipFile(apk).read('classes.dex')
u4=lambda p:struct.unpack_from('<I',d,p)[0]
u2=lambda p:struct.unpack_from('<H',d,p)[0]
def str_at(off):
 while d[off]&128:off+=1
 off+=1
 end=d.index(0,off)
 return d[off:end].decode('utf-8','replace')
strings=[str_at(u4(u4(60)+i*4)) for i in range(u4(56))]
types=[strings[u4(u4(68)+i*4)] for i in range(u4(64))]
def desc(proto):
 p=u4(76)+proto*12
 off=u4(p+8)
 params=[] if not off else [types[u2(off+4+j*2)] for j in range(u4(off))]
 return '('+''.join(params)+')'+types[u4(p+4)]
api={n.get('name'):n for n in ET.parse(args.api_versions).getroot().findall('class')}
localparents={}
for i in range(u4(96)):
 p=u4(100)+i*32;superidx=u4(p+8)
 if superidx!=0xffffffff:localparents[types[u4(p)][1:-1]]=types[superidx][1:-1]
def lookup(cls,name,kind,seen=None):
 seen=set() if seen is None else seen
 if cls in seen:return None
 seen.add(cls)
 node=api.get(cls)
 if node is None:
  return lookup(localparents[cls],name,kind,seen) if cls in localparents else None
 for child in node.findall(kind):
  if child.get('name')==name:return max(int(node.get('since','1')),int(child.get('since',node.get('since','1'))))
 for parent in node.findall('extends')+node.findall('implements'):
  found=lookup(parent.get('name'),name,kind,seen)
  if found is not None:return found
 return None
violations=[]; checked=0;unknown=[]
for i in range(u4(88)):
 p=u4(92)+i*8;cls=types[u2(p)][1:-1];name=strings[u4(p+4)]+desc(u2(p+2))
 level=lookup(cls,name,'method')
 if level is not None:
  checked+=1
  if level>23:violations.append((cls,name,level))
 elif cls.startswith(('android/','java/','org/json/')):unknown.append((cls,name))
for i in range(u4(80)):
 p=u4(84)+i*8;cls=types[u2(p)][1:-1];name=strings[u4(p+4)]
 level=lookup(cls,name,'field')
 if level is not None:
  checked+=1
  if level>23:violations.append((cls,name,level))
for t in types:
 cls=t[1:-1] if t.startswith('L') else ''
 if cls in api and int(api[cls].get('since','1'))>23:violations.append((cls,'type',api[cls].get('since')))
print('Referenced Android/platform methods and fields checked:',checked)
print('References requiring Android API >23:',violations)
print('Unresolved public platform methods:',unknown)
assert not violations
assert not unknown
