#!/usr/bin/env python3
#  Copyright (C) 2022
#      Data Center, Johannes Gutenberg University Mainz
#
#  This file is part of ESPResSo++.
#
#  ESPResSo++ is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  ESPResSo++ is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program.  If not, see <http://www.gnu.org/licenses/>.

###########################################################################
#                                                                         #
#  ESPResSo++ Python script for a simualtion using ScaFaCoS as a          #
#  long-range force calculator                                            #
#                                                                         #
###########################################################################

"""
Attention!
This example ONLY gives an instruction to ScaFaCoS setup in ESPResSo++.
Results will be INCORRECT for SPC/water. ScaFaCoS only supports monatomic 
gas systems with charges. For simulating a molecular system with nonbonded
exclusion rules, we suggest to add necessary compensation using using 
FixedPairListTypes. One example is given in EMODE=1 (using Ewald)

Usage:
1. Choose EMODE=2
2. Define parameters in ScaFaCoS
     fcs_pot = espressopp.interaction.CoulombScafacos(system, coulomb_prefactor,
     energy_tolerance, num_of_particles, method='p3m')
"""

import sys
import time
import espressopp
import mpi4py.MPI as MPI
import logging
import numpy as np
from espressopp import Real3D, Int3D
from espressopp.tools import gromacs
from espressopp.tools import decomp
from espressopp.tools import timers
#from espressopp.tools.units import *

kB  = 1.3806488 * pow(10,-23) # m^2 * kg * s^-2 * K^-1
Na  = 6.0221413 * pow(10, 23) # mol^-1
amu = 1.6605389 * pow(10,-27)
T2Kalvin= amu*1000000/3/kB

# This example reads in a gromacs water system (SPC/Fw) treated with reaction field. See the corresponding gromacs grompp.mdp paramter file. 
# Output of gromacs energies and esp energies should be the same
shear_rate = 0.0
# simulation parameters (nvt = False is nve)
EMODE=2# 0: generalized reaction field / 1: Ewald (internal) / 2: Scafacos
steps = 1000
check = steps/5
rc    = 0.9  # Verlet list cutoff
skin  = 0.03 
timestep = 0.0005
# parameters to convert GROMACS tabulated potential file
sigma = 1.0
epsilon = 1.0
c6 = 1.0
c12 = 1.0

# GROMACS setup files
grofile = "conf.gro"
topfile = "topol.top"

# this calls the gromacs parser for processing the top file (and included files) and the conf file
# The variables at the beginning defaults, types, etc... can be found by calling
# gromacs.read(grofile,topfile) without return values. It then prints out the variables to be unpacked
defaults, types, atomtypes, masses, charges, atomtypeparameters, bondtypes, bondtypeparams, angletypes, angletypeparams, exclusions, x, y, z, vx, vy, vz, resname, resid, Lx, Ly, Lz =gromacs.read(grofile,topfile)

######################################################################
##  IT SHOULD BE UNNECESSARY TO MAKE MODIFICATIONS BELOW THIS LINE  ##
######################################################################
#types, bonds, angles, dihedrals, x, y, z, vx, vy, vz, Lx, Ly, Lz = gromacs.read(grofile,topfile)
#defaults, types, masses, charges, atomtypeparameters, bondtypes, bondtypeparams, angletypes, angletypeparams, exclusions, x, y, z, vx, vy, vz, Lx, Ly, Lz = gromacs.read(grofile,topfile)

num_particles = len(x)

density = num_particles / (Lx * Ly * Lz)
size = (Lx, Ly, Lz)
print('Box size: ',size)

sys.stdout.write('Setting up simulation ...\n')
system = espressopp.System()
system.rng = espressopp.esutil.RNG()
system.bc = espressopp.bc.OrthorhombicBC(system.rng, size)
system.skin = skin

comm = MPI.COMM_WORLD
nodeGrid = decomp.nodeGrid(comm.size,size,rc,skin)
cellGrid = decomp.cellGrid(size, nodeGrid, rc, skin)
system.storage = espressopp.storage.DomainDecomposition(system, nodeGrid, cellGrid)


# add particles to the system and then decompose
props = ['id', 'pos', 'v', 'type', 'mass', 'q']
allParticles = []
for pid in range(num_particles):
	part = [pid + 1, Real3D(x[pid], y[pid], z[pid]),
			Real3D(vx[pid], vy[pid], vz[pid]), types[pid], masses[pid], charges[pid]]
	allParticles.append(part)
system.storage.addParticles(allParticles, *props)    
system.storage.decompose()

#espressopp.tools.pdb.pdbwrite('input.pdb', system, append=False)
#print(types)
#sys.exit(0)

# setting up GROMACS interaction stuff
# create a force capped Lennard-Jones interaction that uses a verlet list
verletlist  = espressopp.VerletList(system, rc)
verletlist.exclude(exclusions)

interaction = espressopp.interaction.VerletListLennardJonesGromacs(verletlist)

# set up LJ interaction according to the parameters read from the .top file
ljinteraction=gromacs.setLennardJonesInteractions(system, defaults, atomtypeparameters, verletlist,rc)

# set up angle interactions according to the parameters read from the .top file
angleinteractions=gromacs.setAngleInteractions(system, angletypes, angletypeparams)

# set up bonded interactions according to the parameters read from the .top file
bondedinteractions=gromacs.setBondedInteractions(system, bondtypes, bondtypeparams)

if EMODE==0:  #truncated coulomb
    # set up coulomb interactions according to the parameters read from the .top file
    # !! Warning: this only works for reaction-field now!
    qq_interactions=gromacs.setCoulombInteractions(system, verletlist, rc, types, epsilon1=1, epsilon2=80, kappa=0)
elif EMODE>0:
    #  Ewald summation parameters
    coulomb_prefactor = 138.935485
    rspacecutoff   = 0.9 #  rspacecutoff - the cutoff in real space
    alphaEwald     = 2.45399
    kspacecutoff   = 30 #  kspacecutoff - the cutoff in reciprocal space

    if EMODE>=1: #GO with Ewald in ESPR++
        # Add Compensation terms first
        fpl_excl=espressopp.FixedPairList(system.storage)
        fpl_excl.addBonds(exclusions)
        coulombR_potBonded = espressopp.interaction.CoulombMultiSiteCorrectionEwald(coulomb_prefactor, alphaEwald, rspacecutoff)
        coulombR_intBonded = espressopp.interaction.FixedPairListTypesCoulombMultiSiteCorrectionEwald(system,fpl_excl)
        coulombR_intBonded.setPotential(type1=0, type2=0, potential=coulombR_potBonded)
        coulombR_intBonded.setPotential(type1=0, type2=1, potential=coulombR_potBonded)
        system.addInteraction(coulombR_intBonded)
    if EMODE==1:
        coulombR_potEwald = espressopp.interaction.CoulombRSpace(coulomb_prefactor, alphaEwald, rspacecutoff)
        coulombR_intEwald = espressopp.interaction.VerletListCoulombRSpace(verletlist)
        coulombR_intEwald.setPotential(type1=0, type2=0, potential = coulombR_potEwald)
        coulombR_intEwald.setPotential(type1=0, type2=1, potential = coulombR_potEwald)
        coulombR_intEwald.setPotential(type1=1, type2=1, potential = coulombR_potEwald)
        system.addInteraction(coulombR_intEwald)
      
        ewaldK_pot = espressopp.interaction.CoulombKSpaceEwald(system, coulomb_prefactor, alphaEwald, kspacecutoff)
        ewaldK_int = espressopp.interaction.CellListCoulombKSpaceEwald(system.storage, ewaldK_pot)
        system.addInteraction(ewaldK_int)
    else: #GO with FCS
        fcs_pot = espressopp.interaction.CoulombScafacos(system, coulomb_prefactor, 0.001, num_particles,'p3m')
        fcs_int = espressopp.interaction.CellListCoulombScafacos(system.storage, fcs_pot)
        system.addInteraction(fcs_int)

# exlusions, i.e. pairs of atoms not considered for the non-bonded part. Those are defined either by bonds which automatically generate an exclusion. Or by the nregxcl variable
#verletlist.exclude(exclusions)

# langevin thermostat
langevin = espressopp.integrator.LangevinThermostat(system)
langevin.gamma = 2.0
langevin.temperature = 2.4942 # kT in gromacs units
integrator = espressopp.integrator.VelocityVerlet(system)
integrator.addExtension(langevin)
integrator.dt = timestep

# print simulation parameters
print('')
print('number of particles =', num_particles)
print('density = %.4f' % (density))
print('rc =', rc)
print('dt =', integrator.dt)
print('skin =', system.skin)
print('steps =', steps)
print('NodeGrid = %s' % (nodeGrid,))
print('CellGrid = %s' % (cellGrid,))
print('')


# analysis
#configurations = espressopp.analysis.Configurations(system)
#configurations.gather()
temperature = espressopp.analysis.Temperature(system)
pressure = espressopp.analysis.Pressure(system)
pressureTensor = espressopp.analysis.PressureTensor(system)

if (shear_rate>0.0):
  integrator2     = espressopp.integrator.VelocityVerletLE(system,shear=shear_rate,viscosity=False)
else:
  integrator2     = espressopp.integrator.VelocityVerlet(system)
# set the integration step  
integrator2.dt  = timestep
integrator2.step = 0

integrator2.addExtension(langevin)

if shear_rate>0.0:
  system.storage.cellAdjust(shear = True)

print("i*timestep,Eb, EAng, ELj, EQQ, Ek, Etotal, Temperature(real)")
fmt='%5.5f %15.8g %15.8g %15.8g %15.8g %15.8g %15.8f %15.8f\n'
#outfile = open("esp.dat", "w")
start_time = time.process_time()

for i in range(int(check)):
    T = temperature.compute()
    P = pressure.compute()
    Eb = 0
    EAng = 0
    for bd in list(bondedinteractions.values()): Eb+=bd.computeEnergy()
    for ang in list(angleinteractions.values()): EAng+=ang.computeEnergy()
    ELj= ljinteraction.computeEnergy()
    if EMODE==0:
        EQQ= qq_interactions.computeEnergy()
    else:
        EQQ=0.0
    T = temperature.compute()
    Ek = 0.5 * T * (3 * num_particles)
    Etotal = Ek+Eb+EAng+EQQ+ELj
    #outfile.write(fmt%(i*steps/check*timestep,Eb, EAng, ELj, EQQ, Ek, Etotal,T*T2Kalvin))
    print(fmt%(i*steps/check*timestep,Eb, EAng, ELj, EQQ, Ek, Etotal,T*T2Kalvin))
    #espressopp.tools.pdb.pdbwrite("traj.pdb", system, append=True)
#    integrator.run(int(steps/check))
    integrator2.run(int(steps/check))

# print timings and neighbor list information
end_time = time.process_time()
timers.show(integrator2.getTimers(), precision=2)

sys.stdout.write('Integration steps = %d\n' % integrator2.step)
sys.stdout.write('CPU time = %.1f\n' % (end_time - start_time))

