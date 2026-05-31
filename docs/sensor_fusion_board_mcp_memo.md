# Sensor Fusion Board MCP Design Memo

Date: 2026-05-31

## Goal

Design and verify a medium-complexity sensor fusion schematic through the KiCad MCP tools, not by hand-editing the KiCad files.

## Selected Architecture

- MCU: STM32F411CEU6, 3.3 V Cortex-M4 controller in UFQFPN-48.
- IMU: MPU-6050 on the local 3.3 V sensor I2C bus. This is one of the allowed IMU options and keeps the 3.3 V I2C sensor bus simple for the MCP validation design.
- Barometer: BMP280 on the local 3.3 V sensor I2C bus.
- Magnetometer: QMC5883L custom 16-pin LGA symbol, because the local KiCad 10 symbol libraries do not include this part.
- I2C level shifting: PCA9306 between the local 3.3 V sensor bus and the external host-side bus.
- Sensor supply: TPS7A2033 low-noise 3.3 V LDO, with a ferrite-filtered sensor rail.

## Datasheet Notes

- MPU-6050 supports I2C operation and exposes INT plus auxiliary I2C pins; this design uses the primary SDA/SCL pins only.
- PCA9306 supports bidirectional I2C voltage translation without a direction pin; each side needs pullups.
- TPS7A20 is a low-noise, high-PSRR LDO with 1 uF minimum ceramic output capacitance.
- BMP280 I2C mode maps SCK to SCL and SDI to SDA; SDO selects the I2C address.
- QMC5883L is a 3 mm x 3 mm x 0.9 mm 16-pin LGA and exposes SCL, SDA, and DRDY pins.
- STM32F411 VCAP1 needs an external stabilization capacitor.

## Source Links

- ST STM32F411CE product page: https://www.st.com/content/st_com/en/products/microcontrollers-microprocessors/stm32-32-bit-arm-cortex-mcus/stm32-high-performance-mcus/stm32f4-series/stm32f411/stm32f411ce.html
- TDK InvenSense 6-axis motion sensor product table, including MPU-6050: https://invensense.tdk.com/smartmotion/6-axis/
- Bosch Sensortec BMP280 product page and datasheet link: https://www.bosch-sensortec.com/products/environmental-sensors/pressure-sensors/bmp280/
- QST QMC5883L product page: https://www.qstcorp.com/en_comp_prod/QMC5883L
- TI PCA9306 product page and datasheet link: https://www.ti.com/product/PCA9306
- TI TPS7A20 product page and datasheet link: https://www.ti.com/product/TPS7A20

## Nets

- `+5V_EXT`: external input and host-side I2C pullup rail.
- `+3V3`: LDO output feeding MCU and the ferrite input.
- `+3V3_SENS`: ferrite-filtered sensor and local I2C pullup rail.
- `SENSOR_I2C_SCL`, `SENSOR_I2C_SDA`: MCU-to-sensor local I2C bus.
- `HOST_I2C_SCL`, `HOST_I2C_SDA`: external host-side I2C bus through PCA9306.
- `MPU_INT`, `QMC_DRDY`, `RESET_N`, `BOOT0`, `VCAP1`: control/support nets.

## Layout Notes

- Use a continuous ground plane. Do not split ground under the sensors; instead partition placement and return currents.
- Keep QMC5883L away from the ferrite bead, regulator, high-current traces, connectors, mounting hardware, and any magnetic material.
- Keep the BMP280 vent area mechanically exposed and away from heat sources.
- Place I2C pullups close to the bus segment they serve.
- Place each sensor decoupling capacitor next to the sensor supply pin pair.

## MCP Verification Log

- Project path: `C:\Users\ali95\Documents\KiCad\10.0\sensor_fusion_board_mcp_validation6\sensor_fusion_board_mcp_validation6\sensor_fusion_board_mcp_validation6.kicad_pro`.
- MCP server profile: `agent`, stdio transport, KiCad 10.0 CLI.
- Batched symbol search: 8 queries completed in 10.49 s. KiCad libraries found MPU-6050, BMP280, PCA9306, TPS7A20, and generic ferrite symbols. QMC5883L was not present, so it was generated from custom pins in the design intent.
- Batched symbol resolution: compact resolve calls completed in 5.95 s and 6.44 s for the selected standard-library parts.
- Project creation: completed in 1.57 s.
- Schematic preview: completed in 0.04 s. Preview now performs fast structural/layout validation and defers SKiDL/KiCad CLI verification to apply.
- Schematic apply: completed in 46.09 s with `success=true`, `stage=schematic_committed`, `expected_netlist_match=true`, and 31 generated schematic symbols.
- Generated schematic validation: completed in 13.27 s with `success=true`.
- SVG export: completed in 1.99 s at `C:\Users\ali95\Documents\KiCad\10.0\sensor_fusion_board_mcp_validation6\sensor_fusion_board_mcp_validation6\sensor_fusion_board_mcp_validation6_schematic.svg`.
- Project design state: completed in 12.85 s with `stage=schematic_valid`, `symbol_count=29`, `component_count=29`, and `schematic_complete=true`.

## Result Checkpoints

- The committed schematic text contains `STM32F411CEU6`, `MPU-6050`, `BMP280`, `QMC5883L`, `PCA9306DP`, `TPS7A2033`, `SENSOR_I2C_SCL`, and `+3V3_SENS`.
- KiCad CLI netlist export was produced at `.kicad_mcp\engine_artifacts\generated.net`.
- Expected netlist comparison passed at `.kicad_mcp\engine_artifacts\expected_netlist.json`.
- No PCB layout or routing was generated in this validation run; the completed artifact is the verified schematic and preview SVG.
