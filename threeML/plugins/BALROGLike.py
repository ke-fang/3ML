

from threeML.plugins.FermiGBMTTELike import FermiGBMTTELike


from gbm_drm_gen import DRMGenTTE, BALROG_DRM


class BALROGLike(FermiGBMTTELike):

    def __init__(self,name, tte_file, source_intervals, background_selections, trigger_time=None, trigdata_file=None, pos_hist_file=None,cspec_file=None,
                 poly_order=-1, matrix_type=2, free_position=True, unbinned=True, verbose=True):



                 self._free_position = free_position

                 self._is_rsp_set = False

                 # Here we create a DRMGenerator object for this GRB. We must extract the detector name
                 # from the input TTE file to feed to the generator

                 drm_generator = DRMGenTTE(tte_file, time=0., cspecfile=cspec_file, trigdat=trigdata_file, poshist=pos_hist_file, mat_type=matrix_type, occult=True)

                 balrog_rsp = BALROG_DRM(drm_generator,0.,0.)

                 super(BALROGLike, self).__init__(name,
                                                  tte_file=tte_file,
                                                  rsp_file=balrog_rsp,
                                                  source_intervals=source_intervals,
                                                  background_selections=background_selections,
                                                  trigger_time=trigger_time,
                                                  poly_order=poly_order,
                                                  unbinned=unbinned,
                                                  verbose=verbose)


                 # only on the start up


                 self._rsp.set_time(self._event_list.time_intervals.half_time)








    def set_active_time_interval(self, *intervals, **kwargs):

        # we do not call the method IN FermiGBMTTELike because
        # it deals with weighted RSPs. Instead, we skip over it
        # and call EventListLike's method.
        super(FermiGBMTTELike, self).set_active_time_interval(*intervals, **kwargs)


        # now we figure out the mean time. this is not ideal for long
        # time intervals. In principle, we should weight with counts and exposure.
        # this would require a lot of changes to BALROG so we ignore for now

        if not self._startup:
            self._rsp.set_time(self._event_list.time_intervals.half_time)


    def set_model(self, likelihoodModel):


        super(BALROGLike, self).set_model(likelihoodModel)
        if self._free_position:

            for key in self._like_model.point_sources.keys():

                self._like_model.point_sources[key].position.ra.free = True
                self._like_model.point_sources[key].position.dec.free = True

    def get_model(self):


        # Here we update the GBM drm parameters which creates and new DRM for that location
        # we should only be dealing with one source for GBM

        if not self._is_rsp_set:

            for key in self._like_model.point_sources.keys():
                ra = self._like_model.point_sources[key].position.ra.value
                dec =self._like_model.point_sources[key].position.dec.value

            # update the location

            self._rsp.set_location(ra,dec)

            if not self._free_position:

                # if we are not fitting for position, we only need to get the RA and DEC once

                self._is_rsp_set = True

        return super(BALROGLike, self).get_model()
