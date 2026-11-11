/* SPDX-License-Identifier: MIT — native tests, no SDK, radio, GPS or SD. */
#include "solo_core.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

static unsigned tests;
#define RUN(f) do { f(); ++tests; printf("PASS %s\n",#f); } while(0)
static const uint8_t kg[]={1,6,11,36,40,44,48};
static void session_validation(void) {
    assert(solo_session_valid("0123456789abcdef"));
    assert(!solo_session_valid(NULL)); assert(!solo_session_valid(""));
    assert(!solo_session_valid("0123456789ABCDEF"));
    assert(!solo_session_valid("0123456789abcde"));
    assert(!solo_session_valid("0123456789abcdef0"));
    assert(!solo_session_valid("0123456789abcdeg"));
}
static void reject_invalid_start(void) {
    solo_plan_t p;
    assert(!solo_plan_init(&p,SOLO_FULL,false,false,NULL,0));
    assert(!solo_plan_init(&p,SOLO_FULL,true,false,NULL,0));
    assert(!solo_plan_init(&p,SOLO_FULL,true,false,NULL,1));
    assert(!solo_plan_init(&p,(solo_profile_t)3,true,false,kg,7));
    assert(!solo_plan_init(&p,SOLO_KG,true,false,kg,6));
    assert(!p.active && p.count==0);
}
static void kg_exact_order_and_dwell(void) {
    solo_plan_t p;
    assert(solo_plan_init(&p,SOLO_KG,true,false,solo_full_candidates,SOLO_MAX_CHANNELS));
    assert(p.count==7);
    for (size_t i=0;i<7;++i) {
        assert(p.channels[i].channel==kg[i]);
        assert(p.channels[i].dwell_ms==(i<3 ? 110 : 100));
    }
    solo_plan_start(&p,1000);
    assert(p.phase==SOLO_WIFI && p.deadline_us==111000);
    for (size_t i=0;i<7;++i) {
        assert(p.index==i);
        assert(!solo_plan_tick(&p,p.deadline_us-1));
        assert(solo_plan_tick(&p,p.deadline_us));
    }
    assert(p.phase==SOLO_WAIT && p.cycles==1);
    assert(p.wifi_complete_us==731000);
    assert(p.deadline_us==2231000);
    assert(!solo_plan_tick(&p,2230999));
    assert(solo_plan_tick(&p,2231000));
    assert(p.phase==SOLO_WIFI && p.index==0 && p.deadline_us==2341000);
}
static void kg_ble_after_five_complete_cycles(void) {
    solo_plan_t p;
    assert(solo_plan_init(&p,SOLO_KG,true,true,kg,7));
    solo_plan_start(&p,0);
    for (unsigned cycle=1;cycle<=5;++cycle) {
        for (size_t ch=0;ch<7;++ch) assert(solo_plan_tick(&p,p.deadline_us));
        assert(p.cycles==cycle);
        if (cycle<5) {
            assert(p.phase==SOLO_WAIT);
            assert(solo_plan_tick(&p,p.deadline_us));
        }
    }
    uint64_t wifi_end=p.wifi_complete_us;
    assert(p.phase==SOLO_BLE && p.deadline_us==wifi_end+1000000);
    assert(!solo_plan_tick(&p,p.deadline_us-1));
    assert(solo_plan_tick(&p,p.deadline_us));
    assert(p.phase==SOLO_WAIT && p.deadline_us==wifi_end+1500000);
    assert(solo_plan_tick(&p,p.deadline_us)); assert(p.phase==SOLO_WIFI);
}
static void full_spectrum_tiers_and_alternation(void) {
    solo_plan_t p;
    assert(solo_plan_init(&p,SOLO_FULL,true,true,solo_full_candidates,SOLO_MAX_CHANNELS));
    assert(p.count==42);
    unsigned total=0;
    for (size_t i=0;i<p.count;++i) total+=p.channels[i].dwell_ms;
    assert(total==14250);
    solo_plan_start(&p,0);
    for (size_t i=0;i<42;++i) assert(solo_plan_tick(&p,p.deadline_us));
    assert(p.phase==SOLO_BLE && p.cycles==1 && p.wifi_complete_us==14250000);
    assert(p.deadline_us==15250000);
    assert(solo_plan_tick(&p,p.deadline_us));
    assert(p.phase==SOLO_WIFI && p.index==0);
}
static void full_uses_only_accepted_channels(void) {
    const uint8_t accepted[]={52,1,36,255,14};
    solo_plan_t p;
    assert(solo_plan_init(&p,SOLO_FULL,true,false,accepted,5));
    assert(p.count==4);
    assert(p.channels[0].channel==1 && p.channels[1].channel==14 &&
           p.channels[2].channel==36 && p.channels[3].channel==52);
    solo_plan_start(&p,0);
    for (size_t i=0;i<4;++i) assert(solo_plan_tick(&p,p.deadline_us));
    assert(p.cycles==1 && p.phase==SOLO_WIFI && p.index==0);
}
static void ble_solo_continuous_without_wifi_channels_or_gps(void) {
    solo_plan_t p;
    assert(solo_plan_init(&p,SOLO_FULL,false,true,NULL,0));
    solo_plan_start(&p,100);
    assert(p.phase==SOLO_BLE && p.deadline_us==UINT64_MAX && p.count==0);
    assert(!solo_plan_tick(&p,UINT64_C(500000000000)));
}
static void late_tick_does_not_fake_missing_dwells(void) {
    solo_plan_t p;
    assert(solo_plan_init(&p,SOLO_KG,true,false,kg,7));
    solo_plan_start(&p,0);
    assert(solo_plan_tick(&p,10000000));
    assert(p.index==1 && p.cycles==0 && p.deadline_us==10110000);
}
static void stop_cancels_every_phase_without_restart(void) {
    for (int phase=SOLO_WIFI;phase<=SOLO_WAIT;++phase) {
        solo_plan_t p;
        assert(solo_plan_init(&p,SOLO_KG,true,true,kg,7)); solo_plan_start(&p,0);
        p.phase=(solo_phase_t)phase;
        solo_plan_stop(&p);
        assert(!p.active && p.phase==SOLO_OFF);
        assert(!solo_plan_tick(&p,UINT64_MAX));
    }
}
static void raw_name_sanitization_is_bounded_and_valid_utf8(void) {
    char out[33];
    const uint8_t valid[]={0x57,0x44,0x47,0xC5,0x82};
    solo_display_name(out,valid,sizeof(valid));
    assert(!memcmp(out,valid,sizeof(valid)) && out[5]==0);
    const uint8_t bad[]={0xFF,0,0xE0,0x80,0x80,0xED,0xA0,0x80};
    solo_display_name(out,bad,sizeof(bad)); assert(!strcmp(out,"????????"));
    const uint8_t incomplete[]={0xF0,0x9F};
    solo_display_name(out,incomplete,sizeof(incomplete)); assert(!strcmp(out,"??"));
    uint8_t long_name[64]; memset(long_name,'A',sizeof(long_name));
    solo_display_name(out,long_name,sizeof(long_name)); assert(strlen(out)==32);
    solo_display_name(out,NULL,0); assert(!out[0]);
}
int main(void) {
    RUN(session_validation); RUN(reject_invalid_start);
    RUN(kg_exact_order_and_dwell); RUN(kg_ble_after_five_complete_cycles);
    RUN(full_spectrum_tiers_and_alternation); RUN(full_uses_only_accepted_channels);
    RUN(ble_solo_continuous_without_wifi_channels_or_gps);
    RUN(late_tick_does_not_fake_missing_dwells); RUN(stop_cancels_every_phase_without_restart);
    RUN(raw_name_sanitization_is_bounded_and_valid_utf8);
    printf("%u native core tests passed; no RF, GPS or SD tested.\n",tests);
    return 0;
}
