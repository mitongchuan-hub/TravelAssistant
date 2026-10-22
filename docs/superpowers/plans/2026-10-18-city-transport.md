# City outing transport implementation plan

Approved design: require driving or public transit beside lodging, retain the existing transport_mode field, allow edits in outing settings, and expose the preference to planning.

- [ ] Add regression coverage for lodging confirmation, saved requirements, planning brief, and rejected missing/invalid modes.
- [ ] Add transport_mode to CITY_FORM_FIELDS and city_requirements validation and changes.
- [ ] Add a required select beside lodging using existing form styling; retain selection after errors and on edit.
- [ ] Expose transport_mode in the city planning brief and document mode-specific route queries in city prompts.
- [ ] Run focused pytest using E:/anaconda/envs/TravelAssistant/python.exe and check rendered templates and diff.

Preserve existing unrelated workspace changes. Legacy city trips can still generate with an unknown mode, but form submission requires an explicit choice.
