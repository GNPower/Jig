# Test fixture: a ps7_init.tcl in the layout Vivado exports, written by hand
# for silicon version 3.0 only. With PS_CLK = 50 MHz it sets the ARM PLL to
# 1300 MHz, the DDR PLL to 1050 MHz and the IO PLL to 1000 MHz; CPU_6x4x to
# 650 MHz with the 6:2:1 ratio; UART 50 MHz, Quad-SPI 200 MHz, SPI 166.67 MHz,
# CAN 100 MHz, PCAP 200 MHz, FCLK0 100 MHz and FCLK1 50 MHz.

proc ps7_pll_init_data_3_0 {} {
    mask_write 0XF8000008 0x0000FFFF 0x0000DF0D
    # ARM PLL: FDIV 26
    mask_write 0XF8000110 0x003FFFF0 0x000FA220
    mask_write 0XF8000100 0x0007F000 0x0001A000
    mask_write 0XF8000100 0x00000010 0x00000010
    mask_write 0XF8000100 0x00000001 0x00000001
    mask_write 0XF8000100 0x00000001 0x00000000
    mask_poll 0XF800010C 0x00000001
    mask_write 0XF8000100 0x00000010 0x00000000
    # ARM_CLK_CTRL: ARM PLL / 2
    mask_write 0XF8000120 0x1F003F30 0x1F000200
    # DDR PLL: FDIV 21
    mask_write 0XF8000114 0x003FFFF0 0x0012C220
    mask_write 0XF8000104 0x0007F000 0x00015000
    mask_write 0XF8000104 0x00000010 0x00000010
    mask_write 0XF8000104 0x00000001 0x00000001
    mask_write 0XF8000104 0x00000001 0x00000000
    mask_poll 0XF800010C 0x00000002
    mask_write 0XF8000104 0x00000010 0x00000000
    mask_write 0XF8000124 0xFFF00003 0x0C200003
    # IO PLL: FDIV 20
    mask_write 0XF8000118 0x003FFFF0 0x001452C0
    mask_write 0XF8000108 0x0007F000 0x00014000
    mask_write 0XF8000108 0x00000010 0x00000010
    mask_write 0XF8000108 0x00000001 0x00000001
    mask_write 0XF8000108 0x00000001 0x00000000
    mask_poll 0XF800010C 0x00000004
    mask_write 0XF8000108 0x00000010 0x00000000
    mask_write 0XF8000004 0x0000FFFF 0x0000767B
}

proc ps7_clock_init_data_3_0 {} {
    mask_write 0XF8000008 0x0000FFFF 0x0000DF0D
    # LQSPI: IO PLL / 5
    mask_write 0XF800014C 0x00003F31 0x00000501
    # UART: IO PLL / 20
    mask_write 0XF8000154 0x00003F33 0x00001403
    # SPI: IO PLL / 6
    mask_write 0XF8000158 0x00003F33 0x00000603
    # CAN: IO PLL / 10 / 1
    mask_write 0XF800015C 0x03F03F33 0x00100A03
    # PCAP: IO PLL / 5
    mask_write 0XF8000168 0x00003F31 0x00000501
    # FPGA0: IO PLL / 10 / 1, FPGA1: IO PLL / 20 / 1
    mask_write 0XF8000170 0x03F03F30 0x00100A00
    mask_write 0XF8000180 0x03F03F30 0x00101400
    mask_write 0XF80001C4 0x00000001 0x00000001
    mask_write 0XF800012C 0x01FFCCCD 0x01EC044D
    mask_write 0XF8000004 0x0000FFFF 0x0000767B
}

proc ps7_ddr_init_data_3_0 {} {
    mask_write 0XF8000008 0x0000FFFF 0x0000DF0D
    mask_write 0XF8006000 0x0001FFFF 0x00000080
    mask_write 0XF8006004 0x0007FFFF 0x00001081
    mask_write 0XF8000B6C 0x00007FFF 0x00000209
    mask_write 0XF8000B70 0x00000021 0x00000021
    mask_write 0XF8000B70 0x00000021 0x00000020
    mask_write 0XF8000B70 0x07FEFFFF 0x00000823
    mask_poll 0XF8000B74 0x00002000
    mask_write 0XF8006000 0x0001FFFF 0x00000081
    mask_poll 0XF8006054 0x00000007
    mask_write 0XF8000004 0x0000FFFF 0x0000767B
}

proc ps7_mio_init_data_3_0 {} {
    mask_write 0XF8000008 0x0000FFFF 0x0000DF0D
    mask_write 0XF8000700 0x00003FFF 0x00001600
    mask_write 0XF8000704 0x00003FFF 0x00000702
    mask_write 0XF8000004 0x0000FFFF 0x0000767B
}

proc ps7_peripherals_init_data_3_0 {} {
    mask_write 0XF8000008 0x0000FFFF 0x0000DF0D
    mask_write 0XF8000B48 0x00000180 0x00000180
    mask_write 0XF8000004 0x0000FFFF 0x0000767B
    mwr -force 0XE0001034 0x00000006
    mask_write 0XE0001018 0x0000FFFF 0x0000007C
    mask_delay 0XF8F00200 1
}

proc ps7_post_config_3_0 {} {
    mask_write 0XF8000008 0x0000FFFF 0x0000DF0D
    mask_write 0XF8000900 0x0000000F 0x0000000F
    mask_write 0XF8000240 0xFFFFFFFF 0x00000000
    mask_write 0XF8000004 0x0000FFFF 0x0000767B
}

proc mask_poll { addr mask } {
    set count 1
    set curval "0x[string range [mrd -value $addr] end-8 end]"
    set maskedval [expr {$curval & $mask}]
    while { $maskedval == 0 } {
        set curval "0x[string range [mrd -value $addr] end-8 end]"
        set maskedval [expr {$curval & $mask}]
        set count [ expr { $count + 1 } ]
    }
}

proc ps7_init {} {
    set si_ver [ps_version]
    if { $si_ver == "3.0" } {
        ps7_mio_init_data_3_0
        ps7_pll_init_data_3_0
        ps7_clock_init_data_3_0
        ps7_ddr_init_data_3_0
        ps7_peripherals_init_data_3_0
    } else {
        puts "this fixture holds silicon version 3.0 only"
    }
}
