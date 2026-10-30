"""Regression for requested effects surviving loading and a newly validated player."""
import struct
import unittest
from app import Panel
from errors import IssueQueue
import test_core as fixtures

class Variable:
    def __init__(self, value): self.value=value
    def get(self): return self.value
    def set(self,value): self.value=value

class Root:
    def after(self,*args): pass

class Persistence(unittest.TestCase):
    def fixture(self):
        g,memory=fixtures.Safeguards.health_game()
        state={'owner':1000,'transient':False}
        for owner in (1000,4000):
            struct.pack_into('<ff',memory,owner+0x60,700,700)
            struct.pack_into('<ff',memory,owner+0x70,850,850)
            struct.pack_into('<ff',memory,owner+0xd0,100,120)
        def context():
            if state['transient']:
                state['transient']=False
                raise RuntimeError('玩家对象正在切换')
            return {'attributes':state['owner']}
        g.context=context
        g.token=lambda owner:(owner,owner+1,2000,owner+2)
        g.targets=lambda feature:[(state['owner'],state['owner']+{'health_floor':0xd80,'health':0x60,'attack':0xd0}[feature],'<ff')]
        panel=Panel.__new__(Panel)
        panel.game=g;panel.root=Root()
        panel.floor_lock=Variable(False);panel.auto_health=Variable(False)
        panel.health_status=Variable('');panel.message=Variable('')
        panel.values=Variable('');panel.connection=Variable('')
        panel.metrics={}
        panel.refresh=lambda:None
        # Step 4 fields: fixture bypasses Panel.__init__, but tick()/fail() need them.
        panel.issues=IssueQueue();panel.issue_log=[];panel.rollback_warned=False
        panel.issue_text=Variable('')
        panel.issue_label=type('Label',(),{'configure':lambda self,**kw:None})()
        return panel,g,memory,state

    def test_temporary_player_switch_does_not_turn_off_requested_floor(self):
        panel,g,memory,state=self.fixture()
        g.start_floor_lock();panel.floor_lock.set(True)
        state['transient']=True
        panel.health_tick()
        panel.health_tick()
        self.assertTrue(panel.floor_lock.get(),'A temporary loading frame turned off the user request')
        self.assertIn('health_floor',g.undo)

    def test_attack_applies_to_new_validated_player_without_touching_old_owner(self):
        panel,g,memory,state=self.fixture()
        g.modifier('attack',20)
        old=bytes(memory[1000+0xd0:1000+0xd8])
        state['owner']=4000
        for _ in range(4):panel.tick()
        self.assertEqual(struct.unpack_from('<ff',memory,4000+0xd0),(2000,2400))
        self.assertEqual(bytes(memory[1000+0xd0:1000+0xd8]),old)

    def test_new_player_floor_rebinds_after_stability_without_old_owner_writes(self):
        panel,g,memory,state=self.fixture()
        g.start_floor_lock();panel.floor_lock.set(True)
        old=bytes(memory[1000+0xd80:1000+0xd88])
        state['owner']=4000
        panel.health_tick()
        self.assertEqual(struct.unpack_from('<ff',memory,4000+0xd80),(0,0))
        panel.health_tick()
        self.assertEqual(struct.unpack_from('<ff',memory,4000+0xd80),(850,850))
        self.assertTrue(panel.floor_lock.get())
        self.assertEqual(bytes(memory[1000+0xd80:1000+0xd88]),old)

    def test_dead_new_player_is_not_healed_or_written(self):
        panel,g,memory,state=self.fixture()
        g.start_floor_lock();panel.floor_lock.set(True)
        g.modifier('attack',20)
        panel.effect_session()
        state['owner']=4000
        struct.pack_into('<ff',memory,4000+0x60,0,0)
        before=bytes(memory);writes=len(g.writes)
        for _ in range(4):panel.health_tick();panel.tick()
        self.assertTrue(panel.floor_lock.get())
        self.assertEqual(bytes(memory),before)
        self.assertEqual(len(g.writes),writes)

    def test_attack_recalculation_uses_fixed_target_without_compounding(self):
        panel,g,memory,state=self.fixture()
        g.modifier('attack',20)
        for _ in range(8):
            struct.pack_into('<ff',memory,1000+0xd0,100,120)
            panel.tick()
            self.assertEqual(struct.unpack_from('<ff',memory,1000+0xd0),(2000,2400))
        g.restore_modifiers()
        self.assertEqual(struct.unpack_from('<ff',memory,1000+0xd0),(100,120))

    def test_manual_restore_clears_attack_intent_and_stays_restored(self):
        panel,g,memory,state=self.fixture()
        g.modifier('attack',20)
        panel.tick()
        panel.effect_session().clear_attack()
        g.restore_modifiers()
        for _ in range(4):panel.tick()
        self.assertEqual(struct.unpack_from('<ff',memory,1000+0xd0),(100,120))
        self.assertNotIn('attack',g.undo)

    def test_returning_to_previous_identity_restores_original_baseline(self):
        panel,g,memory,state=self.fixture()
        g.modifier('attack',20)
        panel.tick()
        state['owner']=4000
        panel.tick();panel.tick()
        state['owner']=1000
        panel.tick();panel.tick()
        self.assertEqual(struct.unpack_from('<ff',memory,1000+0xd0),(2000,2400))
        panel.effect_session().clear_attack()
        g.restore_modifiers()
        self.assertEqual(struct.unpack_from('<ff',memory,1000+0xd0),(100,120))

    def test_restore_during_player_replacement_cancels_floor_request(self):
        panel,g,memory,state=self.fixture()
        g.start_floor_lock();panel.floor_lock.set(True)
        state['owner']=4000
        panel.restore()
        for _ in range(4):panel.health_tick()
        self.assertFalse(panel.floor_lock.get())
        self.assertEqual(struct.unpack_from('<ff',memory,4000+0xd80),(0,0))

    def test_returning_identity_keeps_baseline_if_target_validation_temporarily_fails(self):
        panel,g,memory,state=self.fixture()
        g.modifier('attack',20)
        panel.tick()
        state['owner']=4000
        panel.tick();panel.tick()
        state['owner']=1000
        panel.tick()
        targets=g.targets
        g.targets=lambda feature: (_ for _ in ()).throw(RuntimeError('暂时不可读'))
        panel.tick()
        g.targets=targets
        for _ in range(3):panel.tick()
        self.assertEqual(struct.unpack_from('<ff',memory,1000+0xd0),(2000,2400))
        panel.effect_session().clear_attack()
        g.restore_modifiers()
        self.assertEqual(struct.unpack_from('<ff',memory,1000+0xd0),(100,120))

if __name__=='__main__':unittest.main()
