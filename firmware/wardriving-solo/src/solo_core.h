/* SPDX-License-Identifier: MIT */
#ifndef WDG_SOLO_CORE_H
#define WDG_SOLO_CORE_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define SOLO_MAX_CHANNELS 42
#define SOLO_KG_GAP_US UINT64_C(1500000)
#define SOLO_BLE_BURST_US UINT64_C(1000000)
typedef enum { SOLO_FULL, SOLO_KG } solo_profile_t;
typedef enum { SOLO_OFF, SOLO_WIFI, SOLO_BLE, SOLO_WAIT } solo_phase_t;
typedef struct { uint8_t channel; uint16_t dwell_ms; } solo_channel_t;
typedef struct {
    solo_profile_t profile;
    bool wifi, ble, active;
    solo_phase_t phase;
    solo_channel_t channels[SOLO_MAX_CHANNELS];
    size_t count, index;
    uint64_t deadline_us, wifi_complete_us, cycles;
} solo_plan_t;

extern const uint8_t solo_full_candidates[SOLO_MAX_CHANNELS];
bool solo_session_valid(const char *session);
uint16_t solo_full_dwell_ms(uint8_t channel);
/* False means invalid flags or an incomplete KG profile; never shorten KG. */
bool solo_plan_init(solo_plan_t *plan, solo_profile_t profile, bool wifi, bool ble,
                    const uint8_t *accepted, size_t count);
void solo_plan_start(solo_plan_t *plan, uint64_t now_us);
/* True only when a radio transition/channel change is due. Late ticks do not
 * fabricate completed dwells: each subsequent dwell starts at the actual tick. */
bool solo_plan_tick(solo_plan_t *plan, uint64_t now_us);
void solo_plan_stop(solo_plan_t *plan);
/* Safe display UTF-8, with raw bytes retained separately by the adapter. */
void solo_display_name(char out[33],const uint8_t *bytes,size_t length);
#endif
