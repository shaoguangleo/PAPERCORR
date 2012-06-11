#! /usr/bin/env python
import corr
import pylibmc,time,struct,sys,os,numpy


def exit_fail():
    print 'FAILURE DETECTED.'
    print 'Unexpected error', sys.exc_info()
    try:
        p.disconnect_all()
    except: pass
    exit()

def exit_clean():
    try:
        p.disconnect_all()
    except: pass
    exit()

if __name__ == '__main__':
    from optparse import OptionParser

    o = OptionParser()
    o.set_usage('init_corr.py [options] CONFIG_FILE')
    o.set_description(__doc__)
    o.add_option('-p', '--skip_prog', dest='prog_fpga', action='store_false', default=True,
            help = 'Skip FPGA programming (assumes already programmed). Default: program the FPGAs')
    o.add_option('-c', '--coeff', dest='coeff', type='int', default=700,
            help='Equalisation coefficient.')
    

    opts,args = o.parse_args(sys.argv[1:])

    if args==[]:
        print 'Please specify a configuration file! \nExiting.'
        exit()
    
    prog_fpga=opts.prog_fpga

lh = corr.log_handlers.DebugLogHandler()

try:
    print 'Connecting...',
    p = corr.corr_functions.Correlator(args[0],lh)
    for s,server in enumerate(p.config['servers']): p.loggers[s].setLevel(10)
    mcache = pylibmc.Client(['localhost'])
    print 'done.'


    if prog_fpga:
        print 'Clearing the FPGAs...',
        sys.stdout.flush()
        p.deprog_all()
        time.sleep(2)
        print 'done.'

        #Progam the device
        print 'Programing the FPGA with %s...'%p.config['bitstream'],
        sys.stdout.flush()
        p.prog_all()
        time.sleep(2)
        print 'done.'
    else: print 'Skipped programming FPGAs.'

    print '\n===================='
    print 'Initial configuration'
    print '====================='

    print('Determining number of inputs in the system and setting fengine type register...')
    if p.config['bitstream'] == 'roachf_1024ch_ma_2012_May_30_1841.bof':
        sys.stdout.flush()
        corr_types = [16,32,64,128,256,512]
        ninputs = p.config['n_ants']*2
        print('      This is a %d input correlator system, with %d actual roach boards.'%(ninputs,len(p.fpgas)))
        mux_values = corr_types.index(ninputs)   #0 if 16input, 1 if 32 inputs, 2 if 64 inputs...5 if 512
        for f, fpga in enumerate(p.fpgas):
            fpga.write_int('n_inputs', mux_values)
    print('done,')
    
    #Disable 10gbe cores until network has been set up.Do reset as well.
    #feng ctl bits... 20 = gbe_gpu_disable, 18 = gbe_sw_disable, 30 = gbe_sw_rst, 31 = gbe_gpu_rst
    #10GbE cores are held in rst.
    print('\n Pausing 10GbE data exchange and draining loopback fifos...')
    sys.stdout.flush()
    p.feng_ctrl_set_all(gbe_gpu_disable=True, gbe_sw_disable=True)
    time.sleep(1)
    p.feng_ctrl_set_all(gbe_gpu_disable=True, gbe_sw_disable=True, gbe_sw_rst=True, gbe_gpu_rst=True)
    time.sleep(1)
    print 'done.'

    #Syncing up FEngines
    print 'Syncing F engines...',
    sys.stdout.flush()
    ready = ((int(time.time()*10)%10) == 5)
    while not ready:
        ready = ((int(time.time()*10)%10) == 5)

    p.feng_ctrl_set_all(arm_rst='pulse')

    trig_time = numpy.ceil(time.time())
    print('Armed. Expect trigg at %s local.'%(time.strftime('%H:%M:%S',time.localtime(trig_time)))),
    mcache.set('roachf_init_time', str(trig_time))        
    print ('done')

    #Set antennae base.
    print 'Setting antennae base',
    for f, fpga in enumerate(p.fpgas):
        #slice of the antbase bits in packetiser has offset antbits for some reason (=2), Hence the shift by two bits.
        fpga.write_int('ant_base',(f*4))
    print ('Done')

    #set fft shift and noise sources. make this command line optional eventually.
    #XXX
    print ('Setting fft shift schedule, and noise sources'),
    for i, f in enumerate(p.fpgas):
        f.write_int('fft_shift', p.config['fft_shift'])
        f.write_int('input_selector', 0x33333333)
        #mcache.set('pf%d:fft_shift'%i, opts.fft_shift)
    p.fpgas[2].write_int('input_selector',0x33333333)
    print 'done.'

    #configure network stuff.
    print('Configuring network stuff...'),
    sys.stdout.flush()
    for i, fpga in enumerate(p.fpgas):
        fpga.write('my_ip',struct.pack('>I',(p.config['10gbe_sw_ip'] + i)))
        fpga.write('gbe_sw_port',struct.pack('>I',p.config['10gbe_sw_port']))
        fpga.write('gpu_ip',struct.pack('>I', p.config['gpu_ips'][i]))
        fpga.write('gpu_port',struct.pack('>I', p.config['10gbe_gpu_port']))
        fpga.write_int('ip_base', p.config['10gbe_sw_ip'])

    print('done')

    #configure eq coefficients.
    #XXX make this option.
    print 'Configuring eq coeficients...',
    for fpga in p.fpgas:
        coefficients = numpy.ones(1024, dtype=numpy.int32)*(opts.coeff<<7)#binary point at 7bits
        coeffsbe = coefficients.byteswap()
        cstr = coeffsbe.tostring()
        for ant in range(4):
            fpga.write('eq_%d_coeffs'%ant, cstr)
    print 'done.'
            
        
    
    #Configure 10GbE cores and install tgtap drivers.
    print ('Configuring the 10GbE cores...'),
    sys.stdout.flush() 
    gbe_sw_ip_base = p.config['10gbe_sw_ip'] 
    gbe_sw_port = p.config['10gbe_sw_port']
    arp_table = [0 for i in range(256)]
    arp_table[-1] = (2**48)-1
    #pre- populating the arp tables
    for i in range(ninputs/8):
        mac, ip, port = p.get_roach_gbe_conf(gbe_sw_ip_base, i, gbe_sw_port)
        print mac, ip 
        arp_table[ip%256] = mac

    print arp_table
    for i, fpga in enumerate(p.fpgas):
        mac, ip, port = p.get_roach_gbe_conf(gbe_sw_ip_base,i,gbe_sw_port)
        fpga.config_10gbe_core('switch_gbe3', mac, ip, port, arp_table)
    

    gbe_gpu_ip = p.config['10gbe_gpu_ip']
    gbe_gpu_port = p.config['10gbe_gpu_port']

    arp_table = [(2**48)-1 for i in range(256)]
    for i, fpga in enumerate(p.fpgas):
        #get roach gbe autoincrements the ip address. We don't want that for the 10gbe gpu cores. We ignore the ip address given to us by this function.
        mac, ipignore, port = p.get_roach_gbe_conf(gbe_gpu_ip, i, gbe_gpu_port)
        #for v1 of 10gbe core...
        arp_table[gbe_gpu_ip%256]=mac
        fpga.config_10gbe_core('gpu_gbe2',mac,gbe_gpu_ip,port,arp_table)
        #fpga.tap_start('gbe_gpu','2GPU_gbe_gpu',mac, ip, port)

    print('waiting 30 seconds for arp to complete'),
    time.sleep(30)
    print('done')


    #Restart 10GbE exchange....
    print('Starting 10Gbe data exchange...'),
    sys.stdout.flush()

    #rst 
    p.feng_ctrl_set_all(loopback_mux_rst='pulse')
    p.feng_ctrl_set_all(gbe_gpu_disable=True, gbe_sw_disable=True, gbe_sw_rst=True, gbe_gpu_rst=True)
    p.feng_ctrl_set_all(gbe_gpu_disable=True, gbe_sw_disable=True, gbe_sw_rst=False, gbe_gpu_rst=True)
    p.feng_ctrl_set_all(gbe_gpu_disable=True, gbe_sw_disable=False, gbe_sw_rst=False, gbe_gpu_rst=True)
    p.feng_ctrl_set_all(gbe_gpu_disable=True, gbe_sw_disable=False, gbe_sw_rst=False, gbe_gpu_rst=False)
    p.feng_ctrl_set_all(gbe_gpu_disable=False, gbe_sw_disable=False, gbe_sw_rst=False, gbe_gpu_rst=False)

    print 'done.'


    print('resetting error counters.'),
    p.feng_ctrl_set_all(cnt_rst='pulse')
    print 'done.'


except(KeyboardInterrupt):
    exit()
