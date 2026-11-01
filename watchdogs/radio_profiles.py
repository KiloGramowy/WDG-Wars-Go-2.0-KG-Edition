"""Offline RF catalog and GPS policy. This module never opens or transmits radio.

GPS establishes an area, not a network's settings. AUTO uses only an explicit
user approval for a named local circle, independently for each protocol.
RF and chat channels/keys are intentionally stored in different configurations.
"""
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import time

VERIFIED_ON = '2026-10-29'
MT_SOURCE = 'https://github.com/meshtastic/firmware/blob/332b5b0a2c4fec2896de3c5fa63f46d5cd05ea98/src/mesh/RadioInterface.cpp'
ES_SOURCE = 'https://meshtastic.es/docs/guias-basicas/configuracion-inicial/'
MC_SOURCE = 'https://api.meshcore.nz/api/v1/config'


def _profile(protocol, label, region, frequency, bw, sf, cr, source,
             evidence='documentation; physical RF pending', legacy=False):
    return dict(protocol=protocol, label=label, region=region,
                frequency_hz=frequency, bandwidth_hz=bw, sf=sf,
                coding_rate_denominator=cr, source=source, verified_on=VERIFIED_ON,
                evidence=evidence, legacy=legacy)


PROFILES = {
    'sfnarrow': _profile('meshtastic','ES SFNarrow','EU_868',869618000,62500,7,5,ES_SOURCE,
                         'San Fernando public RF RX verified 2026-10-29; no coverage boundary'),
    'longfast': _profile('meshtastic','EU/UK LongFast','EU_868',869525000,250000,11,5,MT_SOURCE),
    'us_longfast': _profile('meshtastic','USA LongFast slot 20','US',906875000,250000,11,5,MT_SOURCE),
    'us_longturbo': _profile('meshtastic','USA LongTurbo slot 14','US',908750000,500000,11,8,MT_SOURCE),
    'eu_uk_narrow': _profile('meshcore','EU/UK WDG legacy CR5','EU_868',869618000,62500,8,5,
                             'existing WDG configuration; not current official Narrow',
                             'preserved existing configuration; local RF not revalidated',True),
    'eu_uk_narrow_v2': _profile('meshcore','EU/UK Narrow CR8','EU_868',869618000,62500,8,8,MC_SOURCE),
    'eu_uk_default': _profile('meshcore','EU/UK Deprecated Wide','EU_868',869525000,250000,11,5,MC_SOURCE),
    'us_recommended': _profile('meshcore','USA Recommended','US',910525000,62500,7,5,MC_SOURCE),
    'us_ca_narrow': _profile('meshcore','US/CA WDG legacy Narrow','US',910525000,62500,7,5,
                             'existing WDG configuration','preserved; preamble 16; not new official preset',True),
    'us_ca_default': _profile('meshcore','US/CA WDG legacy Wide','US',910525000,250000,11,5,
                              'existing WDG configuration','legacy, local network not verified',True),
    'anz_narrow': _profile('meshcore','AU/NZ WDG legacy','ANZ',915525000,62500,7,5,
                           'existing WDG configuration','outside verified AUTO geography',True),
    'in_narrow': _profile('meshcore','India WDG legacy','IN',865525000,62500,7,5,
                          'existing WDG configuration','outside verified AUTO geography',True),
}


def get_meshtastic_rf(key):
    """Probe-compatible RF dict; channel metadata is a documented default only.

    The live manager uses its independent channel_id for cryptography/chat.
    Explicit US frequencies use the documented name-derived default slots;
    changing the user's public channel never silently changes this RF choice.
    """
    from meshtastic_rx_probe import get_profile
    entry = PROFILES[key]
    if entry['protocol'] != 'meshtastic':
        raise KeyError(key)
    result = get_profile('sfnarrow' if key == 'sfnarrow' else 'longfast')
    result.update({k:entry[k] for k in ('region','frequency_hz','bandwidth_hz','sf','coding_rate_denominator')})
    result.update(profile_id=key, profile=entry['label'])
    return result


def distance_km(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a,*b))
    value = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 12742 * math.asin(min(1, math.sqrt(value)))


def _resolve(lat, lon):
    from .radio_geography import resolve_area
    return resolve_area(lat,lon)


class RadioProfileController:
    """Persist preferences/approvals; propose changes only after fresh stable GPS.

    Backend acknowledgment is separate from selection. Stale GPS or ambiguous
    overlapping approvals yields no proposal and leaves active RF untouched.
    """
    FIX_MAX_AGE = 10.0
    STABLE_SECONDS = 20.0
    STABLE_SAMPLES = 5
    COUNTRY_MARGIN_KM = .25
    LAND_BORDER_MARGIN_KM = 2.0
    AREA_RADIUS_KM = 2.0
    AREA_MARGIN_KM = .2

    def __init__(self, home=None, initial=None, resolver=None, clock=None):
        if home is None:
            from .lora_manager import _user_home
            home = _user_home()
        self.path = Path(home)/'.watchdogs_radio_profiles.json'
        self._clock = clock or time.monotonic
        self._resolve = resolver or _resolve
        defaults = {'meshcore':'eu_uk_narrow','meshtastic':'sfnarrow'}
        defaults.update(initial or {})
        self.data = {'version':1,'protocols':{
            p:{'mode':'manual','manual_profile':k,'active_profile':k}
            for p,k in defaults.items()}, 'bindings':[]}
        self.error = ''
        self._blocked_file = False
        if self.path.exists():
            try:
                candidate=json.loads(self.path.read_text(encoding='utf-8'))
                self._validate(candidate)
                self.data=candidate
            except (OSError,ValueError,KeyError,TypeError) as exc:
                self.error='Radio settings invalid; original file preserved'
                self._blocked_file=True
        self._area = None
        self._fix = None
        self._anchor = None
        self._last_epoch = None
        self._candidate = None
        self._candidate_since = 0
        self._samples = 0
        self._stable = False
        self._gps_status = 'AUTO requires a GPS fix'
        self._had_fix = False

    @staticmethod
    def _validate(data):
        if data['version']!=1 or not isinstance(data['bindings'],list):
            raise ValueError('invalid version/bindings')
        for protocol in ('meshcore','meshtastic'):
            state=data['protocols'][protocol]
            if state['mode'] not in ('auto','manual'):
                raise ValueError('invalid mode')
            for field in ('manual_profile','active_profile'):
                if PROFILES[state[field]]['protocol']!=protocol:
                    raise ValueError('cross-protocol profile')
        ids=set()
        for binding in data['bindings']:
            if not isinstance(binding['id'],str) or binding['id'] in ids or not isinstance(binding['label'],str):
                raise ValueError('invalid area identity')
            ids.add(binding['id'])
            profile=PROFILES[binding['profile']]
            if binding['protocol']!=profile['protocol'] or binding['region']!=profile['region']:
                raise ValueError('invalid approval protocol/region')
            if not isinstance(binding['country'],str) or len(binding['country'])!=2:
                raise ValueError('invalid approval country')
            if not all(isinstance(binding[k],(float,int)) and math.isfinite(binding[k])
                       for k in ('lat','lon','radius_km')):
                raise ValueError('invalid approval geometry')
            if not (-90<=binding['lat']<=90 and -180<=binding['lon']<=180 and binding['radius_km']==2):
                raise ValueError('invalid approval extent')
            if binding.get('approved_by_user') is not True:
                raise ValueError('unapproved area')

    def _save(self):
        if self._blocked_file:
            raise ValueError(self.error)
        self._validate(self.data)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temporary=self.path.with_suffix('.tmp')
        fd=os.open(temporary,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as stream:
                json.dump(self.data,stream,indent=2,sort_keys=True)
                stream.flush();os.fsync(stream.fileno())
            os.chmod(temporary,0o600)
            if getattr(os,'geteuid',lambda:1)()==0 and os.environ.get('SUDO_USER'):
                import pwd
                user=pwd.getpwnam(os.environ['SUDO_USER'])
                os.chown(temporary,user.pw_uid,user.pw_gid)
            os.replace(temporary,self.path)
        finally:
            if temporary.exists():temporary.unlink()

    def _change(self, callback):
        old=deepcopy(self.data)
        try:
            callback()
            self._save()
        except Exception:
            self.data=old
            raise

    def choices(self, protocol):
        return [k for k,v in PROFILES.items() if v['protocol']==protocol]

    def set_manual(self, protocol, key):
        if key not in self.choices(protocol):raise ValueError('profile belongs to another protocol')
        def change():self.data['protocols'][protocol].update(mode='manual',manual_profile=key)
        self._change(change)

    def set_auto(self, protocol):
        self._change(lambda:self.data['protocols'][protocol].update(mode='auto'))

    def applied(self, protocol, key):
        if key not in self.choices(protocol):raise ValueError('invalid active profile')
        if self.data['protocols'][protocol]['active_profile']!=key:
            self._change(lambda:self.data['protocols'][protocol].update(active_profile=key))

    def update(self, fix, now=None):
        now=self._clock() if now is None else now
        acceptable=False
        try:
            acceptable=(fix is not None and all(math.isfinite(fix[k]) for k in ('lat','lon','observed_at','hdop'))
                        and -90<=fix['lat']<=90 and -180<=fix['lon']<=180
                        and 0<=now-fix['observed_at']<=self.FIX_MAX_AGE
                        and fix['quality'] in (1,2,4,5) and fix['sats']>=4 and 0<fix['hdop']<=3)
        except (KeyError,TypeError,ValueError):pass
        if not acceptable:
            self._fix=None;self._stable=False;self._candidate=None;self._samples=0
            self._gps_status=('GPS fix lost; profile kept' if self._had_fix else 'AUTO requires a GPS fix')
            return
        point=(fix['lat'],fix['lon'])
        if self._anchor is not None:
            elapsed=fix['observed_at']-self._anchor['observed_at']
            previous_point=(self._anchor['lat'],self._anchor['lon'])
            # Re-reading the same fix is harmless; changing its coordinates
            # under the same epoch must not bypass jump/stability checks.
            conflict=(elapsed==0 and point!=previous_point)
            jumped=(elapsed>0 and distance_km(point,previous_point)>max(.1,elapsed*.10))
            if elapsed<0 or conflict or jumped:
                self._stable=False;self._candidate=None;self._samples=0;self._fix=None
                self._gps_status='GPS reading rejected; profile kept'
                return
        area=self._resolve(*point)
        self._fix=dict(fix);self._anchor=dict(fix);self._had_fix=True
        self._area=area
        if area is None:
            self._stable=False;self._candidate=None;self._samples=0
            self._gps_status='Unsupported area; select MANUAL'
            return
        if (area['boundary_distance_km']<self.COUNTRY_MARGIN_KM
                or area.get('land_boundary_distance_km') is None
                or area['land_boundary_distance_km']<self.LAND_BORDER_MARGIN_KM):
            self._stable=False;self._candidate=None;self._samples=0
            self._gps_status='Near area boundary; profile kept'
            return
        candidate=area['code']
        if candidate!=self._candidate:
            self._candidate=candidate;self._candidate_since=fix['observed_at'];self._samples=0
        if self._last_epoch!=fix['observed_at']:
            self._samples+=1;self._last_epoch=fix['observed_at']
        self._stable=(self._samples>=self.STABLE_SAMPLES and fix['observed_at']-self._candidate_since>=self.STABLE_SECONDS)
        self._gps_status='GPS fix confirmed' if self._stable else 'Stabilizing GPS (20 s)'

    def _fresh_stable(self):
        return (self._stable and self._fix is not None and
                0<=self._clock()-self._fix['observed_at']<=self.FIX_MAX_AGE)

    def _matches(self, protocol):
        if not self._fresh_stable():return [],False
        matches=[];boundary=False
        for binding in self.data['bindings']:
            profile=PROFILES.get(binding['profile'],{})
            if (binding['protocol']!=protocol or binding['country']!=self._area['code']
                    or binding['region']!=self._area['region']
                    or profile.get('protocol')!=protocol or profile.get('region')!=self._area['region']):
                continue
            distance=distance_km((self._fix['lat'],self._fix['lon']),(binding['lat'],binding['lon']))
            if distance<binding['radius_km']-self.AREA_MARGIN_KM:matches.append(binding)
            elif distance<=binding['radius_km']+self.AREA_MARGIN_KM:boundary=True
        return matches,boundary

    def target(self, protocol):
        state=self.data['protocols'][protocol]
        if state['mode']=='manual':return state['manual_profile']
        matches,boundary=self._matches(protocol)
        keys={b['profile'] for b in matches}
        return next(iter(keys)) if not boundary and len(keys)==1 else None

    def status(self, protocol):
        state=dict(self.data['protocols'][protocol])
        matches,boundary=self._matches(protocol)
        keys={b['profile'] for b in matches}
        gps_status=self._gps_status
        if self._fix is not None and not 0<=self._clock()-self._fix['observed_at']<=self.FIX_MAX_AGE:
            gps_status='GPS fix lost; profile kept'
        reason=self.error or gps_status
        if self._fresh_stable() and not self.error:
            if boundary:reason='Local area boundary; profile kept'
            elif len(keys)>1:reason='Conflicting assignments; select MANUAL'
            elif not keys:reason='No approved profile; select MANUAL'
            else:reason='Approved area: '+matches[0]['label']
        state.update(region=(self._area['label']+' / '+self._area['region']) if self._area and self._fix else '--',
                     region_code=self._area['code'] if self._area and self._fix else None,
                     gps_status=gps_status,reason=reason)
        return state

    def preview_area(self, protocol):
        if not self._fresh_stable():return None
        matches,boundary=self._matches(protocol)
        # Re-approval updates the original circle, never creeps its extent.
        if len(matches)==1 and not boundary:
            return {k:matches[0][k] for k in ('id','label','country','region','lat','lon','radius_km')}
        lat,lon=round(self._fix['lat'],6),round(self._fix['lon'],6)
        country=self._area['code']
        return dict(id=f'{protocol}:{country}:{lat:.6f}:{lon:.6f}',
                    label=f'{country} {lat:.4f},{lon:.4f} / 2km',
                    country=country,region=self._area['region'],lat=lat,lon=lon,radius_km=2.0)

    def remember(self, protocol, key, preview):
        if key not in self.choices(protocol) or not self._fresh_stable() or preview is None:
            raise ValueError('Saving requires a stable GPS fix')
        # Revalidate the exact geometry shown in the confirmation dialog.
        # Freeze the exact circle displayed to the user. Normal GPS jitter
        # while the dialog is open must not silently move its center or make
        # approval impossible. Still require a fresh fix inside its safe core.
        if (preview.get('country') != self._area['code']
                or preview.get('region') != self._area['region']
                or preview.get('radius_km') != self.AREA_RADIUS_KM
                or not isinstance(preview.get('id'),str)
                or not preview['id'].startswith(protocol+':')
                or distance_km((self._fix['lat'],self._fix['lon']),
                               (preview['lat'],preview['lon'])) >= self.AREA_RADIUS_KM-self.AREA_MARGIN_KM):
            raise ValueError('GPS area changed; reopen confirmation')
        old=next((b for b in self.data['bindings'] if b['id']==preview['id']),None)
        if old and any(old[k]!=preview[k] for k in ('label','country','region','lat','lon','radius_km')):
            raise ValueError('Saved area changed; cancel confirmation')
        if PROFILES[key]['region']!=preview['region']:
            raise ValueError('RF profile does not match the GPS region')
        binding=dict(preview,protocol=protocol,profile=key,approved_by_user=True)
        def change():
            self.data['bindings']=[b for b in self.data['bindings'] if b['id']!=binding['id']]
            self.data['bindings'].append(binding)
        self._change(change)

    def forget_area(self, protocol, preview):
        if preview is None:raise ValueError('No local assignment')
        def change():
            self.data['bindings']=[b for b in self.data['bindings']
                                   if not (b['protocol']==protocol and b['id']==preview['id'])]
        self._change(change)

    def has_area(self, protocol, preview):
        return bool(preview and any(b['protocol']==protocol and b['id']==preview['id']
                                    for b in self.data['bindings']))
