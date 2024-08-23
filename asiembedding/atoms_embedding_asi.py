from asi4py.asecalc import ASI_ASE_calculator
from asi4py.pyasi import triang2herm_inplace, triang_packed2full_hermit
from asiembedding.parallel_utils import root_print, mpi_bcast_matrix_storage, \
    mpi_bcast_integer
import numpy as np
from mpi4py import MPI
from ctypes import cdll, CDLL, RTLD_GLOBAL
from ctypes import POINTER, byref, c_int, c_int64, c_int32, c_bool, c_char_p, c_double, c_void_p, CFUNCTYPE, py_object, cast, byref
import ctypes

def dm_saving_callback(aux, iK, iS, descr, data, matrix_descr_ptr):
    try:
        asi, storage_dict, cnt_dict, label = cast(aux, py_object).value
        data_shape = (asi.n_basis,asi.n_basis) if asi.is_hamiltonian_real else (asi.n_basis,asi.n_basis, 2)

        if (matrix_descr_ptr.contents.storage_type not in {1,2}):
            data = asi.scalapack.gather_numpy(descr, data, data_shape)
        elif (matrix_descr_ptr.contents.storage_type in {1,2}): # ASI_STORAGE_TYPE_TRIL,ASI_STORAGE_TYPE_TRIU
            assert not descr, "default_saving_callback supports only dense full ScaLAPACK arrays"
            assert matrix_descr_ptr.contents.matrix_type == 1, "Triangular packed storage is supported only for hermitian matrices"
            uplo = {1:'L',2:'U'}[matrix_descr_ptr.contents.storage_type]
            data = triang_packed2full_hermit(data, asi.n_basis, asi.is_hamiltonian_real, uplo)

        if data is not None:
            asi.dm_count += 1
            assert len(data.shape) == 2
            storage_dict[(asi.dm_count, iK, iS)] = data.copy()

    except Exception as eee:
        print(f"Something happened in ASI default_saving_callback {label}: {eee}\nAborting...")
        MPI.COMM_WORLD.Abort(1)

def ham_saving_callback(aux, iK, iS, descr, data, matrix_descr_ptr):
    try:
        asi, storage_dict, cnt_dict, label = cast(aux, py_object).value
        data_shape = (asi.n_basis,asi.n_basis) if asi.is_hamiltonian_real else (asi.n_basis,asi.n_basis, 2)

        if (matrix_descr_ptr.contents.storage_type not in {1,2}):
            data = asi.scalapack.gather_numpy(descr, data, data_shape)
        elif (matrix_descr_ptr.contents.storage_type in {1,2}): # ASI_STORAGE_TYPE_TRIL,ASI_STORAGE_TYPE_TRIU
            assert not descr, "default_saving_callback supports only dense full ScaLAPACK arrays"
            assert matrix_descr_ptr.contents.matrix_type == 1, "Triangular packed storage is supported only for hermitian matrices"
            uplo = {1:'L',2:'U'}[matrix_descr_ptr.contents.storage_type]
            data = triang_packed2full_hermit(data, asi.n_basis, asi.is_hamiltonian_real, uplo)

        if data is not None:
            asi.ham_count += 1
            assert len(data.shape) == 2
            storage_dict[(asi.ham_count, iK, iS)] = data.copy()

    except Exception as eee:
        print(f"Something happened in ASI default_saving_callback {label}: {eee}\nAborting...")
        MPI.COMM_WORLD.Abort(1)

class AtomsEmbed():

    def __init__(self, atoms, initial_calc, embed_mask, no_scf=False, ghosts=0, outdir='asi.calc'):
        self.atoms = atoms
        self.initial_embed_mask = embed_mask
        "Sets which layer/layers are set to be ghost atoms"
        self.outdir = outdir

        if isinstance(embed_mask, int):
            "We hope the user knows what they are doing and" \
            "their atoms object is ordered accordingly"
            self.embed_mask = [1]*embed_mask
            self.embed_mask += [2]*(len(atoms)-embed_mask)

        if isinstance(embed_mask, list):
            self.embed_mask = embed_mask
            assert len(atoms)==len(embed_mask), \
                "Length of embedding mask does not match number of atoms"

        self.initial_calc = initial_calc
        self.reorder_atoms_from_embed_mask()
        self.atoms.info['embedding_mask'] = self.embed_mask

        self.truncate = False
        self.density_matrix_in = None
        self.fock_embedding_matrix = None

        self.no_scf = no_scf

        if isinstance(ghosts, int):
            ghosts = [ghosts]
        self.ghost_list = [(at in [ghosts]) for at in self.embed_mask]

    def calc_initializer(self, asi):

        calc = self.initial_calc
        if self.no_scf:
            calc.set(sc_iter_limit=0)

        if self.truncate:
            ghost_list = [ ghst for (idx, ghst) in enumerate(self.ghost_list) if idx in self.basis_info.active_atoms ]
        else:
            ghost_list = self.ghost_list

        calc.write_input(asi.atoms, ghosts=ghost_list)
        self._insert_embedding_region_aims()

    def reorder_atoms_from_embed_mask(self):
        """
        Re-orders atoms to push those in embedding region 1 to the beginning
        :return:
        """

        import numpy as np

        "Check if embedding mask is in the correct order (e.g., [1,1,1,2,2,2])"
        "Ensure the next value is always ge than the current"
        idx_list = np.argsort(self.embed_mask)
        sort_embed_mask = np.sort(self.embed_mask)

        self.embed_mask = sort_embed_mask
        self.atoms = self.atoms[idx_list]

    def _insert_embedding_region_aims(self):
        """Lazy way of placing embedding regions in input file"""
        import os

        cwd = os.getcwd()
        geometry_path = os.path.join(cwd, "geometry.in")
        with open(geometry_path, 'r') as fil:
            lines = fil.readlines()
            mask = [any(s in str(line) for s in ('atom', 'empty')) for line in lines]

        shift = 0
        for idx, maskval in enumerate(mask):
            if maskval:
                embedding = self.atoms.info['embedding_mask'][shift]
                shift += 1
                lines.insert(idx + shift, f'qm_embedding_region {embedding}\n')

        with open(geometry_path, 'w') as fil:
            lines = "".join(lines)
            fil.write(lines)

    def full_mat_to_truncated(self, full_mat):
        """_summary_
        Truncate a given matrix with atomic orbitals basis (n_basis x n_basis) dimensions (e.g., hamiltonian, overlap matrix, density matrices) to only atoms specified in atom_mask. Atoms specified in the active region by  self.embed_mask are always honoured.
        """

        import copy

        # TODO: Set-up for upper-triangular matrices.
        full_basis_atoms = self.basis_info.full_basis_atoms
        full_nbasis = self.basis_info.full_nbasis
        active_atoms = self.basis_info.active_atoms

        basis_mask = [True]*full_nbasis

        for bas_idx, atom in enumerate(full_basis_atoms):
            if atom not in active_atoms:
                basis_mask[bas_idx] = False

        trunc_mat = copy.deepcopy(full_mat)

        # Delete Rows and Cols corresponding to inactive atoms 
        # (ie., inactive atom AOs).
        trunc_mat = np.compress( basis_mask, trunc_mat, axis=0 )
        trunc_mat = np.compress( basis_mask, trunc_mat, axis=1 )

        return trunc_mat

    def truncated_mat_to_full(self, trunc_mat):
        """_summary_
        Expand a truncated matrix (dim: nbasis_active*nbasis_active) to
        the full supermolecular basis (dim: nbasis*nbasis).
        """
        import copy

        # Set to local variables to improve readability
        active_atoms = self.basis_info.active_atoms

        # TODO: Set-up for upper-triangular matrices.
        trunc_basis_min_idx = self.basis_info.trunc_basis_min_idx
        trunc_basis_max_idx = self.basis_info.trunc_basis_max_idx
        full_basis_min_idx = self.basis_info.full_basis_min_idx
        full_basis_max_idx = self.basis_info.full_basis_max_idx

        # Set-up empty matrix to read into
        full_mat = np.zeros(shape=(self.basis_info.full_nbasis, self.basis_info.full_nbasis))

        for atom1 in active_atoms:

            # Skip atoms belonging to region A (or 1) as their basis
            # functions are already included

            for atom2 in active_atoms:
                # Skip core active atom blocks - they are already
                # correctly placed.

                atom2_trunc = np.min(np.where(active_atoms==atom2))
                atom1_trunc = np.min(np.where(active_atoms==atom1))

                trunc_row_min = trunc_basis_min_idx[atom2_trunc]
                trunc_row_max = trunc_basis_max_idx[atom2_trunc]
                trunc_col_min = trunc_basis_min_idx[atom1_trunc]
                trunc_col_max = trunc_basis_max_idx[atom1_trunc]

                full_row_min = full_basis_min_idx[atom2]
                full_row_max = full_basis_max_idx[atom2]
                full_col_min = full_basis_min_idx[atom1]
                full_col_max = full_basis_max_idx[atom1]

                full_mat[full_row_min:full_row_max, full_col_min:full_col_max] = trunc_mat[trunc_row_min:trunc_row_max, trunc_col_min:trunc_col_max]

        return full_mat

    def extract_results(self):
        """
        Extracts results from the DFT code output file that are otherwise unavailable
        within the ASE framework. This may need a separate module if other calculators are
        implemented.
        """

        with open('./'+self.outdir+'/asi.log') as output:

            lines = output.readlines()
            for line in lines:
                outline = line.split()

                if '  | Kinetic energy                :' in line:
                    self.kinetic_energy = float(outline[6])

                if '  | Electrostatic energy          :' in line:
                    self.es_energy = float(outline[6])

                if '  | Sum of eigenvalues            :' in line:
                    self.ev_sum = float(outline[7])

    def run(self, ev_corr_scf=False):
        """Actually performed a given simulation run for the calculator.
            Must be separated for indidividual system calls."""
        import os
        import numpy as np
        from asi4py.asecalc import ASI_ASE_calculator

        root_print(f'Calculation {self.outdir}...')

        if self.truncate and len(self.atoms) != self.basis_info.trunc_natoms:
            self.atoms = self.atoms[self.basis_info.active_atoms]

        self.atoms.calc = ASI_ASE_calculator(os.environ['ASI_LIB_PATH'],
                                        self.calc_initializer,
                                        MPI.COMM_WORLD,
                                        self.atoms,
                                        work_dir=self.outdir)

        #self.atoms.calc.asi.keep_hamiltonian = True
        self.atoms.calc.asi.keep_overlap = True
        #self.atoms.calc.asi.keep_density_matrix = True

        self.atoms.calc.asi.dm_storage = {}
        self.atoms.calc.asi.dm_calc_cnt = {}
        self.atoms.calc.asi.dm_count = 0
        self.atoms.calc.asi.register_dm_callback(dm_saving_callback, (self.atoms.calc.asi, self.atoms.calc.asi.dm_storage, self.atoms.calc.asi.dm_calc_cnt, 'DM calc'))

        self.atoms.calc.asi.ham_storage = {}
        self.atoms.calc.asi.ham_calc_cnt = {}
        self.atoms.calc.asi.ham_count = 0
        self.atoms.calc.asi.register_hamiltonian_callback(ham_saving_callback, (self.atoms.calc.asi, self.atoms.calc.asi.ham_storage, self.atoms.calc.asi.ham_calc_cnt, 'Ham calc'))

        if self.density_matrix_in is not None:
            'TODO: Actual type enforcement and error handling'
            self.atoms.calc.asi.init_density_matrix = {(1,1): np.asfortranarray(self.density_matrix_in)}
        if self.fock_embedding_matrix is not None:
            self.atoms.calc.asi.init_hamiltonian = {(1,1): np.asfortranarray(self.fock_embedding_matrix)}

        E0 = self.atoms.get_potential_energy()
        self.total_energy = E0
        self.basis_atoms = self.atoms.calc.asi.basis_atoms
        self.n_basis = self.atoms.calc.asi.n_basis


        # BROADCAST QUANTITIES ONLY CALCULATED TO THE HEAD NODE
        self.atoms.calc.asi.ham_storage = \
            mpi_bcast_matrix_storage(self.atoms.calc.asi.ham_storage,
                                     self.atoms.calc.asi.n_basis,
                                     self.atoms.calc.asi.n_basis)
        self.atoms.calc.asi.dm_storage = \
            mpi_bcast_matrix_storage(self.atoms.calc.asi.dm_storage,
                                     self.atoms.calc.asi.n_basis,
                                     self.atoms.calc.asi.n_basis)

        self.atoms.calc.asi.dm_count = mpi_bcast_integer(self.atoms.calc.asi.dm_count)
        self.atoms.calc.asi.ham_count = mpi_bcast_integer(self.atoms.calc.asi.ham_count)

        self.extract_results()

        # Within the embedding workflow, we often want to calculate the total energy for a
        # given density matrix without performing any SCF steps. Often, this includes using
        # an input electron density constructed from a localised set of MOs for a fragment
        # of a supermolecule. This density will be far from the ground-state density for the fragment,
        # meaning the output eigenvalues significantly deviate from those of a fully converged density.
        # As the vast majority of DFT codes with the KS-eigenvalues to determine the total
        # energy, the total energies due to the eigenvalues do not formally reflect the
        # density matrix of the initial input for iteration, n=0:
        #
        #    \gamma^{n+1} * H^{total}[\gamma^{n}] \= \gamma^{n} * H^{total}[\gamma^{n}],
        #
        # For TE-only calculations, we do not care about the SCF process - we are treating the
        # DFT code as a pure integrator of the XC and electrostatic energies. As such, we
        # 'correct' the eigenvalue portion of the total energy to reflect the interaction
        # of the input density matrix, as opposed to the first set of KS-eigenvectors resulting
        # from the DFT code.
        if ev_corr_scf:

            if self.truncate:
                self.ev_corr_energy = 27.211384500 * np.trace(self.density_matrix_in @ self.full_mat_to_truncated(self.hamiltonian_total))
            else:
                self.ev_corr_energy = 27.211384500 * np.trace(self.density_matrix_in @ self.hamiltonian_total)

            self.ev_corr_total_energy = self.total_energy - self.ev_sum + self.ev_corr_energy

        self.atoms.calc.asi.close()

    @property
    def hamiltonian_kinetic(self):
        core_idx = self.atoms.calc.asi.ham_count - 1
        if self.truncate:
            return self.truncated_mat_to_full(self.atoms.calc.asi.ham_storage.get((core_idx,1,1)))
        else:
            return self.atoms.calc.asi.ham_storage.get((core_idx,1,1))

    @property
    def hamiltonian_total(self):
        tot_idx = self.atoms.calc.asi.ham_count
        if self.truncate:
            return self.truncated_mat_to_full(self.atoms.calc.asi.ham_storage.get((tot_idx,1,1)))
        else:
            return self.atoms.calc.asi.ham_storage.get((tot_idx,1,1))

    @property
    def hamiltonian_electrostatic(self):
        """_summary_
        Generates
        """
        return self.hamiltonian_total - self.hamiltonian_kinetic

    @property
    def fock_embedding_matrix(self):
        """_summary_
        Represents the Fock embedding matrix used to level-shift/orthogonalise
        the subsystem orbitals of the environment from the active system:
            (1) F^{A-in-B} = h^{core} + g^{hilev}[\gamma^{A}]
                                + v_{emb}[\gamma^{A}, \gamma^{B}] + P_{B}[1]
        where \gamma^{A} is the density matrix for the subystem, A},g[\gamma]
        are the two-electron interaction terms, is the embedding potential matrix,
            (2) v_{emb} = g^{low}[\gamma^{A} + \gamma^{B}] - g^{low}[\gamma^{A}]
        and h_core are the one-electron components of the hamiltonian (kinetic
        energy and nuclear-electron interactions).

        NOTE: For FHI-aims, (1) is not formally constructed fully on the
        wrapper level. Numerical stability in FHI-aims requires that the
        onsite potential per atom exactly cancel, precluding the clean
        separation of the nuclear-electron interactions from the total
        electrostatic matrix. As such, v_{emb} is constructed
        from all potential components.

        Formally, this is exactly the same when the embedded Fock matrix is finally
        constructed in FHI-aims  for the high-level calculation - components of
        F^{A-in-B} are calculated in this function are added to the Hamiltonian
        of FHI-aims before its entry into the eigensolver. As such, removing components of
        the nuclear-potential between atoms of A (included in g^{low}[\gamma^{A}])
        makes perfect sense, as they are are calculated natively within FHI-aims.
        For similar reasons, the kinetic energy componnts of h^{core} may be ignored.

        The final term calculated in the wrapper is then:
            (2) F_{wrapper}^{A-in-B} = H_{emb}^{Tot, lolev}[\gamma^{A} + \gamma^{B}]
             - H_{emb}^{Tot, lolev}[\gamma^{A}] - t_k(\gamma^{A} + \gamma^{B}}
                                  - t_k(\gamma^{A}) + P_{B}
        Where t_k is the kinetic energy contribution to the Hamiltonian and
        H_{emb}^{Tot, lolev}[\gamma] is the total hamiltonian derived from the
        density matrix, gamma at the low-level reference level of thoery.


            [1] Lee, S. et al., Acc. Chem. Res. 2019, 52 (5), 1359–1368.
    Input:
        vemb, np.ndarray: Embedding potential of environment at calculation at
                            low level of theory (ie., first four terms of 
                            equation (2)). (nbasis,nbasis):
        projection_matrix, np.ndarray: Projection matrix to level shift P_B
                            components of the environment upwards relative to
                            the active subsystem (nbasis,nbasis)
    Returns:
        fock_embedding_matrix, np.ndarray: fock_embedding_matrix(nbasis,nbasis)
    """
        return self._fock_embedding_matrix

    @fock_embedding_matrix.setter
    def fock_embedding_matrix(self, inp_fock_embedding_mat):

        if (not isinstance(inp_fock_embedding_mat, (np.ndarray)) and (inp_fock_embedding_mat is not None)):
            raise TypeError("Input vemb needs to be np.ndarray of dimensions nbasis*nbasis.")

        if ((inp_fock_embedding_mat is None)):
            self._fock_embedding_matrix = None

        if self.truncate:
            inp_fock_embedding_mat = self.full_mat_to_truncated(inp_fock_embedding_mat)

        self._fock_embedding_matrix = inp_fock_embedding_mat

    @property
    def density_matrix_in(self):
        """_summary_
            Defines the density matrix used as an input to construct the density
            upon the initialisation of a given calulation
        Returns:
            np.ndarray: with dimensions (nbasis,nbasis)
        """
        return self._density_matrix_in

    @density_matrix_in.setter
    def density_matrix_in(self, densmat):

        if (not isinstance(densmat, (list, tuple, np.ndarray))) and (not (densmat is None)):
            raise TypeError("Input needs to be np.ndarray of dimensions nbasis*nbasis.")

        # TODO: DIMENSION CHECKING

        if self.truncate:
            densmat = self.full_mat_to_truncated(densmat)

        self._density_matrix_in = densmat

    @property
    def density_matrices_out(self):
        """_summary_
            Returns a list of all density matrices within the dictionary,
            self.atoms.calc.asi.dm_storage, which stores all the matrices
            return from the calculation via ASI Callbacks.
        Returns:
            list of np.ndarray: with dimensions (nbasis,nbasis)
        """

        try:
            num_densmat = self.atoms.calc.asi.dm_count
        except:
            raise NameError("dm_count = 0: No density matrices stored!")

        out_mats = [ self.atoms.calc.asi.dm_storage.get((dm_num+1,1,1)) \
                     for dm_num in range(num_densmat) ]

        if self.truncate:
            for idx, trunc_mat in enumerate(out_mats):
                out_mats[idx] = self.truncated_mat_to_full(trunc_mat)

        return out_mats

    @property
    def overlap(self):
        return self.atoms.calc.asi.overlap_storage[1,1]

    @property
    def basis_atoms(self):
        return self._basis_atoms

    @basis_atoms.setter
    def basis_atoms(self, val):
        self._basis_atoms = val

    @property
    def n_basis(self):
        return self._n_basis

    @n_basis.setter
    def n_basis(self, val):
        self._n_basis = val

    @property
    def truncate(self):
        return self._truncate

    @truncate.setter
    def truncate(self, val):
        self._truncate = val

    @property
    def basis_info(self):
        return self._basis_info

    @basis_info.setter
    def basis_info(self, val):
        self._basis_info = val