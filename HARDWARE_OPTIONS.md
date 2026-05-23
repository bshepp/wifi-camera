# Hardware Options for WiFi Camera

Research snapshot for future upgrade decisions on the SDR / antenna chain.
Prices and product availability change — re-verify before purchasing.

**Compiled:** 2026-05-23
**Author of this revision:** research session with Claude

---

## Current rig (baseline)

| Item | Spec | Cost |
|---|---|---|
| 2× RTL-SDR NESDR SMArt v5 | 8-bit, 2.56 MSPS, 24 MHz – **1.75 GHz** | ~$60 (pair) |
| HackRF One | 8-bit, 8 MSPS stable, 1 MHz – 6 GHz | ~$300 |
| Webcam | 720p MJPEG | already owned |
| Antennas | mismatched (omni + log-periodic) | n/a |
| **Total** | | **~$360** |

**Hard limitations of this rig:**
- RTL-SDRs **cannot tune 2.4 GHz WiFi natively** — the 915 MHz subsystem in `radar_test_915mhz/` was built specifically to work around this
- No hardware clock sync between devices. Alignment relies on cross-correlation after the fact (see `sync.py`)
- 8-bit ADCs limit dynamic range — passive radar wants ≥12-bit
- 2.4 MHz BW per RTL-SDR is far below a 20/40 MHz WiFi channel
- Mismatched antennas weaken interferometric coherence

---

## Tier 1 — Practical upgrade ($300–$800)

Each option in this tier solves the "can't tune 2.4 GHz coherently" problem in a single purchase.

### AntSDR E200 (AD9361 build) — **recommended**

- **Price:** ~$300–400 (Crowd Supply / direct vendors; ~$364 on AliExpress historically, MicroPhase officially cheaper)
- **Channels:** 2 RX + 2 TX, fully coherent (shared LO)
- **Frequency:** 70 MHz – 6 GHz
- **Bandwidth:** 56 MHz instantaneous (captures a full 802.11n 40 MHz channel + margin)
- **ADC:** 12-bit (~24 dB more dynamic range than current 8-bit chain)
- **Interface:** Gigabit Ethernet (no USB bottleneck)
- **FPGA:** Zynq 7020 SoC — supports on-board pre-processing
- **Why it fits this project:** single device replaces both RTL-SDRs AND HackRF for WiFi work; cross-correlation alignment becomes a sanity check rather than a load-bearing fix

### Pluto+ (Pluto SDR clone, 2T2R)

- **Price:** ~$200–300 (clones from ~$100, ymmv)
- **Channels:** 2 RX + 2 TX coherent
- **Frequency:** 70 MHz – 6 GHz
- **Bandwidth:** 20 MHz (narrower than AntSDR; covers a single 20 MHz 802.11n channel)
- **ADC:** 12-bit (AD9363)
- **Interface:** USB OTG + Gigabit Ethernet
- **Trade-off vs. AntSDR:** cheaper, but the 20 MHz BW means you can't see adjacent-channel context; AD9363 is a downgrade from the AD9361 in the AntSDR

### bladeRF 2.0 micro xA9

- **Price:** ~$720
- **Channels:** 2×2 MIMO
- **Frequency:** 47 MHz – 6 GHz
- **Bandwidth:** 61.44 MHz
- **ADC:** 12-bit, ±1 ppm clock
- **FPGA:** 301KLE Cyclone V — largest in class, real headroom for on-board FFT / correlation
- **When to pick this over AntSDR:** if you want significant FPGA-side processing capability or USB 3.0 vs. GbE matters for your deployment

### KrakenSDR — **good for the 915 MHz subsystem, NOT for WiFi**

- **Price:** ~$350
- **Channels:** 5 phase-coherent RTL-SDRs
- **Frequency:** 24 MHz – **1766 MHz** (same ceiling as current RTL-SDRs)
- **Bandwidth:** 2.4 MHz per channel
- **ADC:** 8-bit
- **Why it doesn't solve the WiFi problem:** the 1766 MHz ceiling is the same hardware limit we're already fighting
- **Where it shines:** drop-in upgrade for `radar_test_915mhz/` — gives 5 hardware-coherent receivers at 915 MHz with built-in calibration

---

## Tier 2 — Prosumer / serious hobbyist ($2k–4k)

### 2× USRP B210 + GPSDO

- **Price:** ~$1500–1600 per B210 + ~$300 board-mounted GPSDO = ~$3.5k for the pair
- **Channels per box:** 2 phase-coherent (shared LO across both AD9361 ADC slices)
- **Two boxes synced via GPSDO + 10 MHz reference:** 4 coherent channels total
- **Frequency:** 70 MHz – 6 GHz
- **Bandwidth:** 56 MHz, 12-bit
- **Why this tier:** first-class GNU Radio / UHD support, mature ecosystem, the "if I get serious" baseline
- **Most academic hobby-scale passive radar work uses this rig or similar**

---

## Tier 3 — Research grade ($20k–40k+)

### 2× USRP X310 + OctoClock-G

- **Per X310:** ~$7–9k with TwinRX or UBX-160 daughtercards
- **OctoClock-G:** ~$1k — 10 MHz reference + PPS distribution, GPSDO-disciplined
- **Channels:** 2–4 RX per box × 2 boxes = 4–8 phase-coherent channels
- **Bandwidth:** up to 200 MHz per channel (captures 80–160 MHz 802.11ac/ax WiFi)
- **ADC:** 14-bit (>70 dB dynamic range)
- **Used by:** UCL, Pisa, and other passive-radar research groups
- **When this makes sense:** publishing PWR papers, working with wide 802.11ac/ax channels, or commercial deployment

---

## Antenna situation

Often overlooked. At 2.4 GHz, λ/2 = 6.25 cm.

| Aspect | Current | Ideal |
|---|---|---|
| Surveillance antennas | mismatched (omni + log-periodic) | matched patch array, calibrated phase |
| Baseline | 38 cm (~6 wavelengths) | λ/2 for unambiguous AoA, or longer with sub-arrays |
| Result | grating lobes, weak coherence | clean AoA, no fold-over ambiguity |
| Upgrade cost | — | $100–200 for 4× matched 2.4 GHz patches + cables + printed mount |

Whatever SDR you buy is wasted without matched antennas. Worth pairing any radio upgrade with this.

---

## Recommendation for this project

Based on the current state (validated 915 MHz subsystem, working capture + sync + correlate + export pipeline, AWS-side processing/training via the SageMaker export tool):

1. **Now:** AntSDR E200 (~$350) + 4× matched 2.4 GHz patch antennas (~$100). Single radio replaces the WiFi-side rig entirely, native 2-channel coherent at 2.4/5 GHz, 12-bit.
2. **If you get serious:** add a second AntSDR E200 for 4 coherent channels, or jump to USRP B210 + GPSDO.
3. **Keep current rig:** for the 915 MHz subsystem and spectrum surveys — already validated there. KrakenSDR is the natural upgrade for that subsystem if/when you want it.

---

## Sources

- [USRP B210 product page (Ettus)](https://www.ettus.com/all-products/ub210-kit/)
- [USRP B210 coherent dual-channel passive radar use case (LinkedIn)](https://www.linkedin.com/pulse/coherent-dual-channel-gated-spectrum-analysis-using-sdr-alyafawi)
- [KrakenSDR (krakenrf.com)](https://www.krakenrf.com/product-page/krakensdr)
- [Pluto+ 2T2R 70 MHz–6 GHz (sdrstore.eu)](https://www.sdrstore.eu/software-defined-radio/instruments/plutosdr/pluto-sdr-ad9363-2t2r-radio-sdr-transceiver-radio-70mhz-6ghz-en)
- [bladeRF 2.0 micro xA9 (Nuand)](https://www.nuand.com/product/bladerf-xa9/)
- [bladeRF 2.0 micro launch coverage (rtl-sdr.com)](https://www.rtl-sdr.com/bladerf-2-0-micro-new-47-mhz-6-ghz-56-mhz-bandwidth-2x2-mimo-sdr-for-480/)
- [AntSDR E200 — Crowd Supply](https://www.crowdsupply.com/microphase-technology/antsdr-e200)
- [AntSDR E200 review (rtl-sdr.com)](https://www.rtl-sdr.com/techminds-reviewing-the-antsdr-e200/)
- [ADALM-PLUTO official specs (Analog Devices wiki)](https://wiki.analog.com/university/tools/pluto/devs/specs)
- [Passive Radar Sensing for Human Activity Recognition (PMC survey)](https://pmc.ncbi.nlm.nih.gov/articles/PMC11342921/)
- [SDR Passive Radar Implementations (HAL paper)](https://hal.science/hal-04741923v1)
- [Ettus GPSDO selection guide](https://kb.ettus.com/GPSDO_Selection_Guide)
