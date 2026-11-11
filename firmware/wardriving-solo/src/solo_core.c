/* SPDX-License-Identifier: MIT */
#include "solo_core.h"
#include <string.h>

const uint8_t solo_full_candidates[SOLO_MAX_CHANNELS] = {
    1,6,11,2,3,4,5,7,8,9,10,12,13,14,
    36,40,44,48,149,153,157,161,165,
    52,56,60,64,100,104,108,112,116,120,124,128,132,136,140,144,169,173,177
};
static const uint8_t kg[] = {1,6,11,36,40,44,48};

bool solo_session_valid(const char *session) {
    if (!session || strlen(session) != 16) return false;
    for (size_t i=0; i<16; ++i)
        if (!((session[i]>='0' && session[i]<='9') ||
              (session[i]>='a' && session[i]<='f'))) return false;
    return true;
}
static bool contains(const uint8_t *channels, size_t count, uint8_t channel) {
    for (size_t i=0; i<count; ++i) if (channels[i]==channel) return true;
    return false;
}
uint16_t solo_full_dwell_ms(uint8_t channel) {
    if (channel==1 || channel==6 || channel==11) return 500;
    if ((channel>=52 && channel<=144) || channel==169 ||
        channel==173 || channel==177) return 250;
    return 400;
}
bool solo_plan_init(solo_plan_t *p, solo_profile_t profile, bool wifi, bool ble,
                    const uint8_t *accepted, size_t count) {
    if (!p || (!wifi && !ble) || (profile!=SOLO_KG && profile!=SOLO_FULL) ||
        count>SOLO_MAX_CHANNELS || (count && !accepted)) return false;
    memset(p,0,sizeof(*p));
    p->profile=profile; p->wifi=wifi; p->ble=ble;
    if (!wifi) return true;
    const uint8_t *order=profile==SOLO_KG ? kg : solo_full_candidates;
    size_t total=profile==SOLO_KG ? sizeof(kg) : SOLO_MAX_CHANNELS;
    for (size_t i=0; i<total; ++i) {
        uint8_t ch=order[i];
        if (!contains(accepted,count,ch)) {
            if (profile==SOLO_KG) { memset(p,0,sizeof(*p)); return false; }
            continue;
        }
        p->channels[p->count++]=(solo_channel_t){ch,
            profile==SOLO_KG ? (ch<=14 ? 110 : 100) : solo_full_dwell_ms(ch)};
    }
    return p->count>0;
}
static void wifi_start(solo_plan_t *p, uint64_t now) {
    p->phase=SOLO_WIFI; p->index=0;
    p->deadline_us=now+(uint64_t)p->channels[0].dwell_ms*1000;
}
void solo_plan_start(solo_plan_t *p, uint64_t now) {
    p->active=true; p->cycles=0; p->wifi_complete_us=0;
    if (p->wifi) wifi_start(p,now);
    else { p->phase=SOLO_BLE; p->deadline_us=UINT64_MAX; }
}
bool solo_plan_tick(solo_plan_t *p, uint64_t now) {
    if (!p || !p->active || now<p->deadline_us) return false;
    if (p->phase==SOLO_WIFI) {
        if (++p->index<p->count) {
            p->deadline_us=now+(uint64_t)p->channels[p->index].dwell_ms*1000;
            return true;
        }
        ++p->cycles; p->wifi_complete_us=now;
        if (p->ble && (p->profile==SOLO_FULL || p->cycles%5==0)) {
            p->phase=SOLO_BLE; p->deadline_us=now+SOLO_BLE_BURST_US;
        } else if (p->profile==SOLO_KG) {
            p->phase=SOLO_WAIT; p->deadline_us=now+SOLO_KG_GAP_US;
        } else wifi_start(p,now);
        return true;
    }
    if (p->phase==SOLO_BLE && p->wifi) {
        uint64_t earliest=p->wifi_complete_us+SOLO_KG_GAP_US;
        if (p->profile==SOLO_KG && now<earliest) {
            p->phase=SOLO_WAIT; p->deadline_us=earliest;
        } else wifi_start(p,now);
        return true;
    }
    if (p->phase==SOLO_WAIT) { wifi_start(p,now); return true; }
    return false;
}
void solo_plan_stop(solo_plan_t *p) {
    if (p) { p->active=false; p->phase=SOLO_OFF; }
}
void solo_display_name(char out[33],const uint8_t *bytes,size_t length) {
    size_t i=0,w=0;
    if (length>32) length=32;
    while (bytes && i<length && w<32) {
        uint8_t a=bytes[i]; size_t n=1;
        bool valid=a>0 && a<0x80;
        if (a>=0xC2 && a<=0xDF) n=2;
        else if (a>=0xE0 && a<=0xEF) n=3;
        else if (a>=0xF0 && a<=0xF4) n=4;
        if (n>1 && i+n<=length) {
            valid=true;
            for (size_t j=1;j<n;++j)
                if ((bytes[i+j]&0xC0)!=0x80) valid=false;
            if ((a==0xE0 && bytes[i+1]<0xA0) ||
                (a==0xED && bytes[i+1]>=0xA0) ||
                (a==0xF0 && bytes[i+1]<0x90) ||
                (a==0xF4 && bytes[i+1]>=0x90)) valid=false;
        }
        if (!valid || w+n>32) { out[w++]='?'; ++i; }
        else { memcpy(out+w,bytes+i,n); i+=n; w+=n; }
    }
    out[w]=0;
}
