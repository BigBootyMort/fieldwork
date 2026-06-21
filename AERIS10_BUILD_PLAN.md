# AERIS-10 Build Plan
Source: https://github.com/NawfalMotii79/PLFM_RADAR  
Variant: **Nexus (3 km)** — recommended starting point. Extended (20 km) multiplies PA boards ×16.

---

## 1. Software — Install First (All Free)

| Tool | Purpose | Download |
|---|---|---|
| Xilinx Vivado 2023.x WebPACK | FPGA synthesis + bitstream | xilinx.com/support/download |
| STM32CubeIDE | MCU firmware build + flash | st.com/stm32cubeide |
| KiCad 7+ | View/edit schematics | kicad.org |
| Python 3.11 (Anaconda) | GUI + utils | anaconda.com |
| Git | Clone repo | git-scm.com |
| FreeCAD or DraftSight | View .dwg mechanical drawings | freecad.org |

**First action:** Clone the repo locally.
```
git clone https://github.com/NawfalMotii79/PLFM_RADAR.git
```

---

## 2. PCBs to Fabricate — Order Early (2–4 week lead time)

All Gerbers are in `4_Schematics and Boards Layout/4_7_Production Files/`.

| Board | Layers | Material | Fab Folder | Notes |
|---|---|---|---|---|
| Main Board | 10 | **Rogers RO4350B** | `Gerber_Main_Board/` | Controlled impedance — upload the PCBWay impedance PDF with order |
| PA Board | 4 | Rogers RO4350B | `Gerber_PA/` | Nexus: order 4. Extended: order 16. |
| Power Board | ~4 | FR4 | `Gerber_PowerBoard/` | Standard material OK |
| Freq Synth Board | 6 | Rogers RO4350B | `Gerber_freq_synth/` | Controlled impedance |

**Recommended fab:** PCBWay (the impedance note in the repo is written for them).  
**Key note:** RO4350B is 3–5× the cost of FR4. Budget ~$80–$200 per board depending on size and quantity.

---

## 3. Components to Order

### 3A. Main Board — Critical ICs (long lead time, order first)

| Qty | Part Number | Description | Source |
|---|---|---|---|
| 4 | ADAR1000ACCZN | 4-ch RF phase shifter/attenuator (10.5 GHz) | Analog Devices / Digi-Key |
| 1 | XC7A50T-2FTG256I | Artix-7 FPGA, 256-LBGA | Xilinx / Digi-Key / Mouser |
| 1 | STM32F746ZGT7 | ARM Cortex-M7 MCU, 144-LQFP | ST / Digi-Key |
| 2 | LTC5552IUDB | 10 GHz upconverter mixer | Analog Devices / Digi-Key |
| 16 | ADTR1107ACCZ | RF front-end gain block | Analog Devices / Digi-Key |
| 1 | AD9484BCPZ-500 | 8-bit 500 MSPS ADC | Analog Devices / Digi-Key |
| 2 | AD8352ACPZ-R7 | 2 GHz differential RF amplifier | Analog Devices / Digi-Key |
| 17 | M3SWA2-34DR+ | SPDT RF switch (Mini-Circuits) | Mini-Circuits / Digi-Key |
| 1 | AD9708AR | 8-bit DAC | Analog Devices / Digi-Key |
| 1 | FT2232HQ | Dual USB UART bridge | FTDI / Digi-Key |
| 1 | MT25QL01GBBB8E12-0AUT | 1Gb SPI NOR flash (BGA-24) | Micron / Digi-Key |
| 1 | AT93C46A-10SQ-2.7 | 1Kb EEPROM, SOIC-8 | Microchip / Digi-Key |
| 1 | EP4RKU+ | Bandpass filter (Mini-Circuits) | Mini-Circuits |
| 2 | BPF2 | Bandpass filter | Check schematic for exact part |

### 3B. Main Board — Other ICs

| Qty | Part Number | Description |
|---|---|---|
| 3 | ADS7830IPWR | 8-bit 8-ch ADC (TI) |
| 2 | DAC5578SRGET | 8-bit DAC (TI) |
| 4 | OPA4703EA/250 | Quad rail-to-rail op-amp (TI) |
| 16 | INA241A3IDGKR | Current sense amplifier (TI) |

### 3C. Main Board — Connectors

| Qty | Part Number | Description |
|---|---|---|
| 37 | 142-0731-211 | SMA female edge-mount, 50Ω |
| 2 | CJT-T-P-HH-ST-TH1 | Twinax female connector |
| 2 | MINI-USB-32005-201 | Mini-USB receptacle |
| 40 | 22-23-2021 | Molex 2-pin header |
| 16 | 22-23-2031 | Molex 3-pin header |
| 1 | MA10-2 | 2-pin connector |
| Various | PINHD-1X2 through PINHD-2X7 | Standard pin headers |

### 3D. Main Board — Passives (order from Digi-Key/LCSC)

All 0201 unless noted. Buy in tape-and-reel strips; 0201 parts are tiny.

**Capacitors (sample — get full list from BOM_Main_Board.xlsx):**
- 71× 0.1µF 0201, 28× 100nF 0402, 34× 100pF 0201, 16× 1µF 0201, 12× 10µF 0805, 2× 4.7µF tantalum EIA3528

**Resistors:**
- 49× 1kΩ 0201, 27× 4.7kΩ 0201, 22× 22.1kΩ 0201, 16× 2.443kΩ 0201, 13× 22Ω 0201 — plus many others (see BOM)

**Inductors:**
- 5× BLM15HB121SN1 (ferrite bead 0402), plus RF inductors in L0201 package

### 3E. Frequency Synthesizer Board — Key ICs

| Qty | Part Number | Description |
|---|---|---|
| 1 | AD9523BCPZ | Low-jitter clock distribution, 14-output, QFN-72 |
| 2 | ADF4382ABCCZ | Fractional-N PLL synthesizer, CC-48 |
| 2 | CVHD-950-50.000 | 50 MHz VCXO oscillator |
| 1 | ECOC-2522-100.000-3HC | 100 MHz temperature-compensated oscillator |
| 4 | ATS1005-3DB-FD-T05 | 3 dB fixed attenuator (SMA) |
| 4 | MTX2-143+ | Bandpass filter (Mini-Circuits) |
| 11 | 142-0731-211 | SMA female connectors |

### 3F. Power Board — Key ICs

| Qty | Part Number | Description |
|---|---|---|
| 21 | TPS562208DDCT | 2A synchronous buck converter (TI) |
| 6 | ADM7151ACPZ-04-R7 | Ultralow-noise LDO 4V output (ADI) |
| 5 | LM2662MX/NOPB | Switched-cap voltage inverter (TI) |
| 2 | TPS7A8300RGRR | Ultralow-noise LDO (TI) |
| 10 | T521W476M020ATE045 | 47µF 20V tantalum cap |
| 2 | VLP8040T-2R2N (2.2µH) | Power inductor |
| 19 | VLP8040T-3R3N (3.3µH) | Power inductor |

---

## 4. Programming & Debug Hardware

| Item | Purpose | Approx. Cost |
|---|---|---|
| Digilent JTAG-HS3 or XUP-USB-JTAG | Flash FPGA (XC7A50T) | $30–$50 |
| ST-Link V3 (STLINK-V3SET) | Flash/debug STM32F746 | $25 |
| USB-UART adapter (FT232 based) | Serial console | $5–$10 |

---

## 5. Tools to Have

### Essential
| Tool | Notes |
|---|---|
| Reflow oven **or** hot-air rework station | Required for FPGA BGA, QFN parts. T-962 oven (~$100) or Hakko FR-810B |
| Soldering iron with fine tips (0.5–1mm) | For through-hole and touch-up |
| Digital microscope or loupe (40×+) | 0201 parts are 0.6×0.3mm — you need magnification |
| ESD mat + wrist strap | Essential for FPGA and ADI parts |
| Multimeter | Voltage checks during power-up |
| Solder paste (Sn63Pb37 or SAC305) | For reflow |
| No-clean flux pen | Touch-up and rework |
| PCB holder / helping hands | |

### Strongly Recommended
| Tool | Notes |
|---|---|
| Oscilloscope (≥200 MHz, 2-ch) | Power rail verification, SPI/UART debug |
| Logic analyzer (8-ch, ≥100 MHz) | FPGA/MCU bus debugging (Saleae Logic or clone) |
| RF power meter or spectrum analyzer | Verify TX at 10.5 GHz — can borrow/rent |
| Hot plate | Pre-heating boards before reflow reduces tombstoning |

---

## 6. Mechanical Parts

From `8_Utils/Mechanical_Drawings/` (all .dwg files — open in FreeCAD):

| Part | DWG File | Notes |
|---|---|---|
| Enclosure | Enclosure.dwg | Custom — send to sheet metal shop or 3D print prototype |
| Slip ring | SlipRing.dwg | Allows 360° continuous rotation — source commercially |
| Heatsink — Main Board | Heat_Sink_Main_Board.dwg | Custom extruded aluminum |
| Heatsink — PA | Heat_Sink_PA.dwg | One per PA board |
| Waveguide upper | Upper_Part_Waveguide.dwg | CNC machined aluminum |
| Waveguide lower | Lower_Part_Waveguide.dwg | CNC machined aluminum |
| Waveguide down | Down_Part_Waveguide.dwg | |

**Additional mechanical:**
- Stepper motor (NEMA 17 likely — confirm from schematics/BOM)
- Stepper driver (matches motor)
- Mounting hardware (M2/M3 screws/standoffs)
- RF coax cable assemblies (SMA-SMA, semi-rigid for RF path)

---

## 7. Order Sequence (Critical Path)

Do these in order — PCBs and long-lead ICs take weeks.

```
Week 0 (now)
  ├─ Install all software
  ├─ Clone repo, open schematics in KiCad
  ├─ Open BOMs in Excel: confirm part numbers before ordering
  └─ Open .dwg files: identify what needs fabrication vs. commercial sourcing

Week 0–1 (order immediately)
  ├─ PCBs → PCBWay (Gerbers + impedance note)
  ├─ ADAR1000, ADF4382, AD9523, LTC5552, ADTR1107 → Digi-Key/Mouser
  │   (Analog Devices parts can have 8–12 week lead times if out of stock)
  ├─ XC7A50T-2FTG256I → Digi-Key (check stock; order from authorized distributor)
  └─ STM32F746ZGT7 → Digi-Key/Mouser

Week 1–2
  ├─ All TI ICs (TPS562208, ADM7151, INA241A3, etc.)
  ├─ FTDI FT2232HQ
  ├─ Mini-Circuits parts (M3SWA2, EP4RKU+, MTX2-143+)
  ├─ Connectors (SMA, headers, USB)
  └─ Crystals/oscillators (CVHD-950, ECOC-2522)

Week 2
  ├─ Passives (caps, resistors, inductors) — fast ship from LCSC or Digi-Key
  ├─ JTAG programmer, ST-Link V3
  └─ Solder paste, flux, consumables

Week 3–4 (when PCBs arrive)
  ├─ Verify boards against schematics
  ├─ Start assembly: Power Board first (bring up voltage rails before anything else)
  ├─ Then Freq Synth Board
  ├─ Then Main Board
  └─ PA Boards last
```

---

## 8. Budget Estimate (Nexus Variant)

| Category | Estimated Cost |
|---|---|
| PCBs (4 boards, RO4350B) | $300–$600 |
| ADAR1000 ×4 | ~$200 |
| XC7A50T FPGA | ~$80–$120 |
| LTC5552 mixers ×2 | ~$80 |
| ADTR1107 ×16 | ~$100 |
| AD9523 + ADF4382 ×2 | ~$150 |
| AD9484 ADC | ~$60 |
| STM32F746 | ~$20 |
| Other ICs (TI, FTDI, etc.) | ~$100 |
| Connectors (37 SMA + others) | ~$150 |
| Passives | ~$50 |
| Mini-Circuits parts | ~$80 |
| Oscillators/crystals | ~$50 |
| Mechanical (enclosure, heatsinks, slip ring) | $100–$300 |
| Stepper motor + driver | ~$30 |
| JTAG + ST-Link programmers | ~$75 |
| Solder paste, consumables | ~$40 |
| **Total estimate** | **$1,600–$2,200** |

Extended variant adds ~12 more PA boards — add $500–$1,000.

---

## 9. Next Steps

1. **Today**: Install software, clone repo
2. **This week**: Open `BOM_Main_Board.xlsx`, `BOM_Freq_Synth.xlsx`, `BOM.xlsx` (PA), `BOM_Power_Board.xlsx` — cross-reference part numbers against current Digi-Key/Mouser stock and lead times
3. **Place PCB order at PCBWay first** — longest lead item
4. **Check repo issues page** — active development, known limitations listed there
5. Come back here and we'll walk through the firmware build environment setup
